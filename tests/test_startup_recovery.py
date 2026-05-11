"""
Regression tests for startup-recovery code paths in src/proxy_addon.py.

These cover the bug classes from recent commits:
- _meta.json corruption + auto-heal via rebuild_meta_from_sidecars
- _queue.json restoration on proxy startup
- corrupted JSON in state/meta/failed should fall back to defaults, not crash
"""
import json
import pytest
from pathlib import Path

import proxy_addon as pa


# ── Per-test isolation ────────────────────────────────────────────────────────
# Same pattern as test_proxy_addon.py: monkeypatch all file globals to tmp_path
# and clear in-process caches before each test.

@pytest.fixture(autouse=True)
def _tmp_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(pa, "CACHE_DIR",   tmp_path)
    monkeypatch.setattr(pa, "META_FILE",   tmp_path / "_meta.json")
    monkeypatch.setattr(pa, "STATE_FILE",  tmp_path / "_state.json")
    monkeypatch.setattr(pa, "FAILED_FILE", tmp_path / "_failed.json")
    monkeypatch.setattr(pa, "QUEUE_FILE",  tmp_path / "_queue.json")
    monkeypatch.setattr(pa, "_state_cache",    {})
    monkeypatch.setattr(pa, "_state_cache_ts", 0.0)
    # _inflight is a module-level set used by _restore_queue_from_file; clear it
    # so leftover entries from earlier tests don't make this run skip URLs.
    monkeypatch.setattr(pa, "_inflight", set())
    # _fetch_queue is a module-level InspectableQueue — replace with a fresh one
    # so restored items don't mingle with the singleton's prior contents.
    monkeypatch.setattr(pa, "_fetch_queue", pa.InspectableQueue())
    # Don't actually start the worker thread when _restore_queue_from_file
    # decides to call _ensure_worker; that would leak threads across tests.
    monkeypatch.setattr(pa, "_ensure_worker", lambda: None)


# Helper to write a sidecar file for a (url, body) pair, mirroring the layout
# that write_cache produces. Returns the path to the cached content file.
def _make_cached_pair(tmp_path: Path, url: str, body: bytes, ct: str = "text/html"):
    cache_path = pa.url_to_path(url)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(body)
    sidecar = pa.sidecar_path(cache_path)
    sidecar.write_text(json.dumps({
        "url": url, "content_type": ct, "content_type_full": ct,
        "status": 200, "size": len(body),
        "cached_at": "2024-01-01T00:00:00+00:00",
    }), "utf-8")
    return cache_path


# ═══════════════════════════════════════════════════════════════════════════════
#  load_meta — corruption fallback
# ═══════════════════════════════════════════════════════════════════════════════

def test_load_meta_missing_returns_defaults(tmp_path):
    # No _meta.json on disk — load_meta() must return the canonical empty shape.
    meta = pa.load_meta()
    assert meta == {"cached_urls": {}, "stats": {"total": 0, "bytes": 0}}

def test_load_meta_corrupt_json_returns_defaults(tmp_path):
    # Simulate a half-written file from a non-atomic crash.
    (tmp_path / "_meta.json").write_text("{ this is not json", "utf-8")
    meta = pa.load_meta()
    # Must not raise, must return the empty default shape — otherwise the
    # dashboard sees an empty index and every cached file vanishes.
    assert meta == {"cached_urls": {}, "stats": {"total": 0, "bytes": 0}}

def test_load_meta_empty_file_returns_defaults(tmp_path):
    (tmp_path / "_meta.json").write_text("", "utf-8")
    assert pa.load_meta() == {"cached_urls": {}, "stats": {"total": 0, "bytes": 0}}

def test_load_failed_corrupt_returns_empty(tmp_path):
    (tmp_path / "_failed.json").write_text("not json", "utf-8")
    assert pa.load_failed() == {}

def test_load_state_corrupt_returns_defaults(tmp_path):
    (tmp_path / "_state.json").write_text("not json", "utf-8")
    state = pa.load_state()
    # Defaults must still be present even if the file is corrupt.
    assert state["offline_mode"] is False
    assert state["fetch_images"] is True


# ═══════════════════════════════════════════════════════════════════════════════
#  rebuild_meta_from_sidecars
# ═══════════════════════════════════════════════════════════════════════════════

def test_rebuild_meta_from_empty_cache(tmp_path):
    # No sidecars at all → rebuild produces the empty shape (0 entries, 0 bytes).
    count = pa.rebuild_meta_from_sidecars()
    assert count == 0
    meta = pa.load_meta()
    assert meta["stats"]["total"] == 0
    assert meta["stats"]["bytes"] == 0

def test_rebuild_meta_from_sidecars_repopulates(tmp_path):
    # Drop two cached files+sidecars on disk, wipe _meta.json, and confirm
    # rebuild reads the sidecars and reconstructs the index.
    body_a = b"<a>"     # 3 bytes
    body_b = b"<bbbb>"  # 6 bytes
    _make_cached_pair(tmp_path, "https://example.com/a.html", body_a)
    _make_cached_pair(tmp_path, "https://example.com/b.html", body_b)
    count = pa.rebuild_meta_from_sidecars()
    assert count == 2
    meta = pa.load_meta()
    assert "https://example.com/a.html" in meta["cached_urls"]
    assert "https://example.com/b.html" in meta["cached_urls"]
    # Bytes must be summed from sidecar size fields.
    assert meta["stats"]["bytes"] == len(body_a) + len(body_b)

def test_rebuild_meta_skips_sidecar_with_no_url(tmp_path):
    # A sidecar that's missing the 'url' key is unusable — must be skipped,
    # not crash, not produce a phantom entry.
    bad = tmp_path / "example.com" / "x.html.meta.json"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text(json.dumps({"size": 5}), "utf-8")
    assert pa.rebuild_meta_from_sidecars() == 0

def test_rebuild_meta_skips_corrupt_sidecar(tmp_path):
    # Make one valid pair and one corrupt sidecar — rebuild must keep going
    # and produce the one valid entry.
    _make_cached_pair(tmp_path, "https://example.com/good.html", b"ok")
    bad = tmp_path / "example.com" / "bad.html.meta.json"
    bad.write_text("{ not json", "utf-8")
    count = pa.rebuild_meta_from_sidecars()
    assert count == 1


# ═══════════════════════════════════════════════════════════════════════════════
#  _check_meta_integrity — the auto-heal trigger
# ═══════════════════════════════════════════════════════════════════════════════

def test_check_meta_integrity_rebuilds_when_meta_lost(tmp_path):
    # Simulate the bug: many sidecars on disk but _meta.json was wiped/corrupted.
    # Need > 10 sidecars and meta_count < 50% of sidecars for the heuristic to fire.
    for i in range(15):
        _make_cached_pair(tmp_path, f"https://example.com/page{i}.html", b"x")
    # Wipe the meta file entirely.
    (tmp_path / "_meta.json").write_text(
        json.dumps({"cached_urls": {}, "stats": {"total": 0, "bytes": 0}}), "utf-8"
    )
    pa._check_meta_integrity()
    meta = pa.load_meta()
    # All 15 URLs should be back in the index after auto-heal.
    assert meta["stats"]["total"] == 15

def test_check_meta_integrity_no_rebuild_when_close_enough(tmp_path):
    # If meta_count >= 50% of sidecar_count, the heuristic should NOT rebuild.
    # We seed meta with the right count to prove the check is gated correctly.
    for i in range(15):
        _make_cached_pair(tmp_path, f"https://example.com/page{i}.html", b"x")
    # Pretend meta already knows about 12 of them — above the 50% threshold.
    meta = {"cached_urls": {f"https://example.com/page{i}.html": {"path": f"example.com/page{i}.html", "size": 1}
                            for i in range(12)},
            "stats": {"total": 12, "bytes": 12}}
    (tmp_path / "_meta.json").write_text(json.dumps(meta), "utf-8")
    pa._check_meta_integrity()
    # Total must be unchanged (no rebuild fired).
    after = pa.load_meta()
    assert after["stats"]["total"] == 12

def test_check_meta_integrity_skips_small_caches(tmp_path):
    # Fewer than 10 sidecars: the heuristic should NOT rebuild even when meta is empty,
    # to avoid noise on fresh installs that just cached 1-2 files.
    for i in range(5):
        _make_cached_pair(tmp_path, f"https://example.com/page{i}.html", b"x")
    (tmp_path / "_meta.json").write_text(
        json.dumps({"cached_urls": {}, "stats": {"total": 0, "bytes": 0}}), "utf-8"
    )
    pa._check_meta_integrity()
    after = pa.load_meta()
    # No rebuild — meta stays at 0 because the cache is below the noise floor.
    assert after["stats"]["total"] == 0


# ═══════════════════════════════════════════════════════════════════════════════
#  _restore_queue_from_file — queue persistence across restarts
# ═══════════════════════════════════════════════════════════════════════════════

def test_restore_queue_missing_file_is_noop(tmp_path):
    # No _queue.json on disk — function must return cleanly with empty queue.
    pa._restore_queue_from_file()
    assert pa._fetch_queue.qsize() == 0

def test_restore_queue_corrupt_file_is_noop(tmp_path):
    (tmp_path / "_queue.json").write_text("{ not json", "utf-8")
    pa._restore_queue_from_file()
    # Must not crash, queue stays empty.
    assert pa._fetch_queue.qsize() == 0

def test_restore_queue_repopulates_in_memory_queue(tmp_path):
    items = [
        {"url": "https://example.com/a", "referer": "", "force": False, "depth": 0, "origin_host": "example.com"},
        {"url": "https://example.com/b", "referer": "", "force": True,  "depth": 1, "origin_host": "example.com"},
    ]
    (tmp_path / "_queue.json").write_text(json.dumps(items), "utf-8")
    pa._restore_queue_from_file()
    urls = [t[0] for t in pa._fetch_queue.snapshot()]
    assert "https://example.com/a" in urls
    assert "https://example.com/b" in urls
    # The 'force' flag should round-trip correctly — important so that
    # explicit re-fetch requests survive a restart.
    for item in pa._fetch_queue.snapshot():
        if item[0] == "https://example.com/b":
            assert item[2] is True

def test_restore_queue_skips_already_cached(tmp_path):
    # If a URL is already on disk, there's no reason to re-download it
    # just because it was still queued at shutdown.
    _make_cached_pair(tmp_path, "https://example.com/cached.html", b"hi")
    items = [
        {"url": "https://example.com/cached.html", "depth": 0},
        {"url": "https://example.com/new.html", "depth": 0},
    ]
    (tmp_path / "_queue.json").write_text(json.dumps(items), "utf-8")
    pa._restore_queue_from_file()
    urls = [t[0] for t in pa._fetch_queue.snapshot()]
    assert "https://example.com/cached.html" not in urls
    assert "https://example.com/new.html" in urls

def test_restore_queue_registers_inflight(tmp_path):
    # Restored items must be added to _inflight to prevent the proxy from
    # double-queueing them when the same URL hits the addon again.
    items = [{"url": "https://example.com/x", "depth": 0}]
    (tmp_path / "_queue.json").write_text(json.dumps(items), "utf-8")
    pa._restore_queue_from_file()
    assert "https://example.com/x" in pa._inflight

def test_restore_queue_skips_items_without_url(tmp_path):
    # Items missing the 'url' key should be silently skipped — they'd crash the
    # worker otherwise.
    items = [
        {"depth": 0},                        # no url
        {"url": "", "depth": 0},             # empty url
        {"url": "https://example.com/ok"},   # valid
    ]
    (tmp_path / "_queue.json").write_text(json.dumps(items), "utf-8")
    pa._restore_queue_from_file()
    urls = [t[0] for t in pa._fetch_queue.snapshot()]
    assert urls == ["https://example.com/ok"]

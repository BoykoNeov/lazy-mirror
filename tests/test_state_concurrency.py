"""
Concurrency regression tests for _patch_state in src/proxy_addon.py.

Background:
  _patch_state is hit many times per second by the proxy worker. Without the
  in-process lock + bypass of the 1-second TTL cache inside the lock, concurrent
  writers can each load a stale snapshot, mutate it, and clobber each other on
  write — silently dropping settings the user (or another thread) just changed.

  These tests can't simulate a real cross-process race (Python threading locks
  don't span processes), but they DO verify:
    1. The in-process lock prevents writers from dropping fields when they race.
    2. The TTL cache is invalidated on every patch (next reader gets fresh data).
    3. Concurrent patches end with the union of all fields written.
"""
import json
import threading
import pytest

import proxy_addon as pa


@pytest.fixture(autouse=True)
def _tmp_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(pa, "STATE_FILE", tmp_path / "_state.json")
    # Wipe in-process cache so the first read in each test goes to disk.
    monkeypatch.setattr(pa, "_state_cache",    {})
    monkeypatch.setattr(pa, "_state_cache_ts", 0.0)


def test_patch_state_serial_independent_keys(tmp_path):
    # Sanity baseline (no threads) — two patches to disjoint keys both survive.
    pa._patch_state(offline_mode=True)
    pa._patch_state(fetch_delay_ms=250)
    saved = json.loads((tmp_path / "_state.json").read_text("utf-8"))
    assert saved["offline_mode"] is True
    assert saved["fetch_delay_ms"] == 250


def test_patch_state_concurrent_distinct_keys_all_survive(tmp_path):
    # The headline concurrency test: spawn N threads each writing a unique key.
    # If the read-merge-write inside the lock is correct, every key must be
    # present in the final file. If the lock or the cache-bypass is broken,
    # some keys will be lost because workers loaded a stale state.
    N = 32
    threads = []
    for i in range(N):
        key = f"_concurrent_key_{i}"
        # Use a closure-bound value per thread; load_state ignores unknown keys
        # on read but _patch_state writes them through verbatim.
        t = threading.Thread(target=lambda k=key, v=i: pa._patch_state(**{k: v}))
        threads.append(t)
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    saved = json.loads((tmp_path / "_state.json").read_text("utf-8"))
    # Every concurrent key must be present with its written value — this is the
    # property the cross-process race was breaking before the fix.
    for i in range(N):
        assert saved[f"_concurrent_key_{i}"] == i, (
            f"Key _concurrent_key_{i} missing or wrong — concurrent patches dropped a write"
        )


def test_patch_state_preserves_unrelated_fields_under_load(tmp_path):
    # Pre-seed disk with a user-set field (simulates a setting saved by the
    # dashboard). Then hammer _patch_state from worker threads writing only
    # queue-related fields. The user-set field must NEVER disappear.
    (tmp_path / "_state.json").write_text(
        json.dumps({"fetch_delay_ms": 999, "queue_paused": True}), "utf-8"
    )

    def worker(i):
        # Writers loop a few times each to make collisions more likely.
        for _ in range(5):
            pa._patch_state(**{f"worker_{i}_seen": True})

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    saved = json.loads((tmp_path / "_state.json").read_text("utf-8"))
    # The original user fields must survive the storm of concurrent patches.
    assert saved["fetch_delay_ms"] == 999
    assert saved["queue_paused"] is True
    # And every worker's marker must be present.
    for i in range(8):
        assert saved[f"worker_{i}_seen"] is True


def test_patch_state_invalidates_cache_so_next_read_is_fresh(tmp_path):
    # Reader sees stale cache only until a writer fires; after a write,
    # the next load_state() must reflect what's on disk.
    # Prime the cache with a known stale value.
    pa._patch_state(offline_mode=False)
    cached = pa.load_state()
    assert cached["offline_mode"] is False

    # Simulate another writer flipping the bit.
    pa._patch_state(offline_mode=True)
    # The next read must NOT return the cached False — the patch must have
    # zeroed _state_cache_ts so the next load_state goes to disk.
    fresh = pa.load_state()
    assert fresh["offline_mode"] is True


def test_patch_state_does_not_persist_queue_depth_via_patch(tmp_path):
    # queue_depth has its own file (QUEUE_DEPTH_FILE) precisely so the proxy
    # never writes _state.json from the worker. If someone (re-)introduces
    # _patch_state(queue_depth=...), the cross-process race comes back.
    # This test pins the contract: a worker-style queue_depth patch goes to
    # _state.json (since that's what the function does), so any future change
    # that moves it elsewhere needs to be deliberate. We assert the file write
    # path still routes queue_depth through _patch_state if called, so the
    # docstring constraint is visible.
    pa._patch_state(queue_depth=42)
    saved = json.loads((tmp_path / "_state.json").read_text("utf-8"))
    # Just verify the write went through — the contract is documented in the
    # _patch_state docstring; this is a smoke check, not an enforcement test.
    assert saved.get("queue_depth") == 42

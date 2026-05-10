"""
Shared test setup for LazyMirror.

proxy_addon.py calls CACHE_DIR.mkdir() at module scope, so LAZYMIRROR_CACHE
must be set to a writable temp directory BEFORE the module is first imported.
This conftest.py runs before any test module, ensuring that order is correct.
"""
import os
import sys
import tempfile
from pathlib import Path

# Set LAZYMIRROR_CACHE before any test module imports proxy_addon or dashboard.
_session_tmp = Path(tempfile.mkdtemp(prefix="lm_tests_"))
os.environ.setdefault("LAZYMIRROR_CACHE", str(_session_tmp))

# Add src/ to sys.path so tests can import proxy_addon and dashboard directly.
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

---
description: Run the full pytest suite (fast, ~1s) and report pass/fail counts
---

Run pytest and surface the result.

```
python -m pytest -q --tb=short
```

If anything fails, show the short traceback and identify which test files are affected. Suite typically passes in under 1 second.

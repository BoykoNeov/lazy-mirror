# docs/dev — Feature Planning

This folder is for short-lived planning docs for **non-trivial features**. A bug
fix or one-file edit does not need a plan; reach for this when the task touches
multiple files, has open design questions, or you want to be able to resume it
in a fresh session.

## Three-file pattern

For each in-progress feature, create a subfolder `docs/dev/<task-name>/` with:

| File | Purpose |
|------|---------|
| `plan.md` | The accepted approach. Updated as the implementation reveals new constraints. |
| `context.md` | Pointers to key files/functions, prior incidents, related issues. The "stuff a fresh session needs to know." |
| `tasks.md` | Bulleted checklist. Tick items off as you go. |

When the feature is shipped, **archive** the folder by moving it to
`docs/dev/_done/<task-name>/` (or delete it — your call). Don't let active
folders accumulate.

## Example

```
docs/dev/
  export-rewriter-v2/
    plan.md         # "Replace string-replace loop with single-pass regex"
    context.md      # "_rewrite_html @ dashboard.py:1640. Prior bug: ..."
    tasks.md        # - [ ] benchmark; - [ ] write regex; - [ ] swap; - [ ] tests
  _done/
    meta-auto-heal/ # shipped 2026-04, archived
```

## When NOT to use

- Single-file bug fixes — just fix them, the commit message is the doc.
- Trivial refactors / renames — git history is enough.
- Throwaway experiments — keep those in a scratch branch, not docs/.

## Slash commands

There are no skill-style commands yet for managing these — just create the
folder manually when a feature warrants planning.

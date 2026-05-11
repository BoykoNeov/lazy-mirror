---
description: Quick AST syntax check on src/proxy_addon.py and src/dashboard.py
---

Run a fast Python AST parse to confirm both source files have valid syntax. UTF-8 is required because dashboard.py contains non-ASCII characters.

```
python -c "import ast,io; ast.parse(io.open('src/proxy_addon.py', encoding='utf-8').read()); print('proxy_addon.py OK')"
python -c "import ast,io; ast.parse(io.open('src/dashboard.py',   encoding='utf-8').read()); print('dashboard.py OK')"
```

Run both via Bash, report the result.

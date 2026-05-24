#!/usr/bin/env python3
from __future__ import annotations

import py_compile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {
    ".git",
    "target",
    "__pycache__",
    ".venv",
    ".ruff_cache",
    ".pytest_cache",
    "runtime",
    "backups",
    "node_modules",
}

failures: list[tuple[str, str]] = []
for path in ROOT.rglob("*.py"):
    rel = path.relative_to(ROOT)
    if any(part in SKIP_DIRS for part in rel.parts):
        continue
    try:
        py_compile.compile(str(path), doraise=True)
    except Exception as exc:  # noqa: BLE001 - CI should report every syntax-style failure.
        failures.append((str(rel), str(exc)))

if failures:
    print("Python syntax check failed:")
    for rel, err in failures:
        print(f"- {rel}: {err.splitlines()[-1] if err else 'unknown error'}")
    raise SystemExit(1)

print("Python syntax check passed.")
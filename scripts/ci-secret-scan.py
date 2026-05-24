#!/usr/bin/env python3
from __future__ import annotations

import re
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
MAX_SCAN_BYTES = 2_000_000
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("api_key_sk", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("google_psid", re.compile(r"g\.a000-[A-Za-z0-9_-]{20,}")),
    ("google_sidts", re.compile(r"sidts-[A-Za-z0-9_-]{20,}")),
    ("google_sidcc", re.compile(r"AKEyXz[A-Za-z0-9_-]{20,}")),
    ("bearer_token", re.compile(r"Authorization\s*[:=]\s*Bearer\s+[A-Za-z0-9._-]{20,}", re.I)),
    ("basic_auth_blob", re.compile(r"Authorization\s*[:=]\s*Basic\s+[A-Za-z0-9+/=]{20,}", re.I)),
]
ALLOW_HINTS = (
    "YOUR_",
    "<",
    ">",
    "example",
    "placeholder",
    "REDACTED",
    "redacted",
    "xxx",
    "your-",
    "${",
    "test",
    "fake",
    "dummy",
)

findings: list[tuple[str, int, str]] = []
for path in ROOT.rglob("*"):
    if not path.is_file():
        continue
    rel = path.relative_to(ROOT)
    if any(part in SKIP_DIRS for part in rel.parts):
        continue
    if path.stat().st_size > MAX_SCAN_BYTES:
        continue
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            text = path.read_text(encoding="latin-1")
        except Exception:
            continue
    except Exception:
        continue
    for lineno, line in enumerate(text.splitlines(), 1):
        if any(hint in line for hint in ALLOW_HINTS):
            continue
        for name, regex in PATTERNS:
            if regex.search(line):
                findings.append((str(rel), lineno, name))

if findings:
    print("Potential secrets found. Values are intentionally not printed.")
    for rel, lineno, name in findings:
        print(f"- {rel}:{lineno}: {name}")
    raise SystemExit(1)

print("Secret scan passed.")
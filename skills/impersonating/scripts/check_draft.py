#!/usr/bin/env python3
"""Check drafts for an optional user preference against dash punctuation."""

from __future__ import annotations

import json
import re
import sys


draft = sys.stdin.read()

violations = {
    "em_dash": draft.count("\N{EM DASH}"),
    "en_dash": draft.count("\N{EN DASH}"),
    "spaced_double_hyphen": len(
        re.findall(r"(?<!-)[ \t]--[ \t](?!-)", draft)
    ),
}
violations = {name: count for name, count in violations.items() if count}

print(
    json.dumps(
        {
            "ok": not violations,
            "violations": violations,
        },
        sort_keys=True,
    )
)
raise SystemExit(0 if not violations else 1)

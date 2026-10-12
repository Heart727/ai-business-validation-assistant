"""Best-effort screening for bounded, fictional demonstration text."""

import re


_DISALLOWED = re.compile(
    r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}"
    r"|(?<!\d)1[3-9](?:[\s-]?\d){9}(?!\d)"
    r"|(?:sk-|app-|dify-)[A-Z0-9_-]{20,}"
    r"|忽略.{0,12}(?:指令|提示)"
    r"|ignore\s+(?:all\s+)?(?:previous|system)\s+instructions",
    re.IGNORECASE,
)


def require_demo_text(value: str) -> str:
    if _DISALLOWED.search(value):
        raise ValueError("Only fictional business data without contacts or credentials is allowed")
    return value

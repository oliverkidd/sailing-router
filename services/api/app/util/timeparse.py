"""UTC datetime parsing for query params (handles URL '+' → space)."""

from __future__ import annotations

from datetime import UTC, datetime


def parse_utc_iso(value: str) -> datetime:
    s = value.strip().replace("Z", "+00:00")
    if "T" in s and " " in s and "+" not in s:
        s = s.replace(" ", "+", 1)
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt

"""The app's notion of "now" in Tehran. `DEMO_NOW` (ISO, e.g. 2026-10-22T18:05) fixes it so demo takes and tests
repeat exactly (spec §0.2 A2); the UI shows the fixed time in the conditions strip when it is set."""

import os
from datetime import datetime

from core.text import TEHRAN

_override: datetime | None = None


def now() -> datetime:
    if _override is not None:
        return _override
    raw = os.environ.get("DEMO_NOW")
    if raw:
        dt = datetime.fromisoformat(raw)
        return dt.replace(tzinfo=TEHRAN) if dt.tzinfo is None else dt.astimezone(TEHRAN)
    return datetime.now(TEHRAN)


def is_demo() -> bool:
    return _override is not None or bool(os.environ.get("DEMO_NOW"))


def set_now(dt: datetime | None) -> None:
    """Tests (and a hidden demo chip) pin the clock; None returns to real time."""
    global _override
    _override = None if dt is None else (dt.replace(tzinfo=TEHRAN) if dt.tzinfo is None else dt.astimezone(TEHRAN))

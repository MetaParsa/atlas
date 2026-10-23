import sys

import pytest

LIMITERS = ("chat_limit", "live_limit", "tile_limit", "map_limit", "turn_hour_limit", "turn_day_limit",
            "new_session_limit", "api_limit")


@pytest.fixture(autouse=True)
def fresh_limits(monkeypatch):
    """Every test client is the same address ("testclient"): give each test its own per-IP budgets."""
    app_mod = sys.modules.get("torob.app")
    if app_mod is not None:
        for name in LIMITERS:
            old = getattr(app_mod, name)
            monkeypatch.setattr(app_mod, name, app_mod.RateLimiter(old.limit, old.window))
        app_mod._busy.clear()
        app_mod._busy_ip.clear()
    yield

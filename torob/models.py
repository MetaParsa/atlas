"""Pydantic models (spec §4 + §0.2 R15). More arrive with S2–S6."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

DEFAULT_AREA = "جمهوری / فردوسی"
DEFAULT_CENTER = (35.6935, 51.4150)


class Location(BaseModel):
    lat: float
    lng: float
    label: str | None = None
    source: Literal["default", "gps", "area", "landmark"] = "default"


class Conditions(BaseModel):
    """The conditions strip; the agent edits the same object from chat, so strip and chat never disagree."""
    center: Location = Field(default_factory=lambda: Location(lat=DEFAULT_CENTER[0], lng=DEFAULT_CENTER[1], label=DEFAULT_AREA))
    radius_km: float | None = 3          # None = کل تهران
    budget_min: int | None = None
    budget_max: int | None = None
    payment: Literal["any", "credit", "credit_in_person"] = "any"
    fast_delivery: bool = False
    open: Literal["any", "now", "at_arrival"] = "at_arrival"
    arrive_at: datetime | None = None    # «ساعت ۸:۳۰ میام»; None = now + travel time
    mode: Literal["walk", "car"] = "walk"


class Need(BaseModel):
    product_type: str = ""
    keywords: list[str] = []
    category_hints: list[str] = []
    budget_min: int | None = None
    budget_max: int | None = None
    use_case: str | None = None
    must_have: list[str] = []
    brand_pref: list[str] = []
    named_product: str | None = None     # the user named a model → skip to shops (R7)
    missing_info: list[str] = []


class Chip(BaseModel):
    """An answer chip. `say` sends `value` as the user's message; `direct_call` runs `tool(args)` with no LLM (A4)."""
    label: str
    value: str = ""
    kind: Literal["say", "direct_call"] = "say"
    tool: str | None = None
    args: dict = {}


class RouterOutput(BaseModel):
    """The one JSON LLM call per free-text turn (R10); code routes on `intent`."""
    intent: Literal["need", "named_product", "refine", "call_shop", "ask_results", "other"] = "other"
    need: Need | None = None
    conditions_patch: dict = {}
    message: str = ""


class SessionState(BaseModel):
    need: Need | None = None
    conditions: Conditions = Field(default_factory=Conditions)
    product_ids: list[str] = []          # last product picks shown
    selected_product_id: str | None = None
    last_block_kind: Literal["none", "products", "shops"] = "none"
    calls: dict[str, dict] = {}          # call_id -> CallResult (S6)
    plan: dict | None = None             # «برنامه‌ی خرید» card (S6)
    history: list[dict] = []             # last turns: {"role", "content"}

    def remember(self, role: str, content: str, keep: int = 10) -> None:
        self.history = (self.history + [{"role": role, "content": content}])[-keep:]

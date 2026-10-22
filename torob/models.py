"""Pydantic models (spec §4 + §0.2 R15). More arrive with S2–S6."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

MAX_TEXT = 1000      # longest user message we accept (keeps prompts and bills bounded)

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
    radius_km: float | None = Field(3, gt=0, le=50, allow_inf_nan=False)   # None = کل تهران
    budget_min: int | None = Field(None, ge=0)
    budget_max: int | None = Field(None, gt=0)
    payment: Literal["any", "credit", "credit_in_person"] = "any"
    fast_delivery: bool = False
    open: Literal["any", "now", "at_arrival"] = "at_arrival"
    arrive_at: datetime | None = None    # «ساعت ۸:۳۰ میام»; None = now + travel time
    mode: Literal["walk", "car"] = "walk"

    @model_validator(mode="after")
    def _budget_order(self):
        if self.budget_min is not None and self.budget_max is not None and self.budget_min > self.budget_max:
            raise ValueError("budget_min > budget_max")
        return self


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
    label: str = Field(max_length=80)
    value: str = Field("", max_length=300)
    kind: Literal["say", "direct_call"] = "say"
    tool: str | None = Field(None, max_length=40)
    args: dict = {}      # validated per tool by the router's registry before any call


class RouterOutput(BaseModel):
    """The one JSON LLM call per free-text turn (R10); code routes on `intent`."""
    intent: Literal["need", "named_product", "refine", "call_shop", "ask_results", "other"] = "other"
    need: Need | None = None
    conditions_patch: dict = {}
    target: str = Field("", max_length=80)   # call_shop: which shown shop («اولی», «دومی», or part of its name)
    message: str = ""


class SessionState(BaseModel):
    need: Need | None = None
    conditions: Conditions = Field(default_factory=Conditions)
    product_ids: list[str] = []          # last product picks shown
    selected_product_id: str | None = None
    shop_ids: list[str] = []             # every shop shown for the selected product (incl. category-only)
    call_ok_ids: list[str] = []          # shops shown as having it — the only ones call_shop accepts
    shops_shown: list[dict] = []         # compact facts of the shown shops (ask_results answers from these)
    tried_shop_ids: list[str] = []       # called and failed (no answer / unavailable): alternatives skip them
    declined_morning: bool = False       # «نه، همین الان» to the next-morning offer: don't ask again
    last_block_kind: Literal["none", "products", "shops"] = "none"
    calls: dict[str, dict] = {}          # call_id -> CallResult (S6)
    plan: dict | None = None             # «برنامه‌ی خرید» card (S6)
    history: list[dict] = []             # last turns: {"role", "content"}

    def remember(self, role: str, content: str, keep: int = 10) -> None:
        self.history = (self.history + [{"role": role, "content": content}])[-keep:]

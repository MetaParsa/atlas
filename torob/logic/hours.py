"""Opening hours from the dataset's `weekly_hours`, in Tehran time.

Iranian week: index 0 = Saturday … 6 = Friday. A day is `declared` (with shifts), `closed`, or `unknown`
(missing, `unsupported`, or declared without a real shift). Unknown is never guessed.
"""

import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

TEHRAN = ZoneInfo("Asia/Tehran")
DAY_KEYS = ["sat", "sun", "mon", "tue", "wed", "thu", "fri"]
ZERO = time(0, 0)


@dataclass
class DayHours:
    status: str                                            # declared | closed | unknown
    shifts: list[tuple[time, time]] = field(default_factory=list)  # end == 00:00 means midnight


def iran_weekday(d: date) -> int:
    """Python Monday=0 → Iranian Saturday=0."""
    return (d.weekday() + 2) % 7


def parse_weekly_hours(raw: str | dict | None) -> dict[int, DayHours]:
    data = json.loads(raw) if isinstance(raw, str) else (raw or {})
    week: dict[int, DayHours] = {}
    for i, key in enumerate(DAY_KEYS):
        day = data.get(key)
        if not day:
            week[i] = DayHours("unknown")
            continue
        status = day.get("status")
        if status == "closed":
            week[i] = DayHours("closed")
            continue
        shifts = []
        for s in day.get("shifts") or []:
            start, end = time.fromisoformat(s["start_time"]), time.fromisoformat(s["end_time"])
            if start == ZERO and end == ZERO:
                continue  # padding the export adds after real shifts
            shifts.append((start, end))
        week[i] = DayHours("declared", shifts) if status == "declared" and shifts else DayHours("unknown")
    return week


def hours_known(week: dict[int, DayHours]) -> bool:
    return any(d.status != "unknown" for d in week.values())


def to_json(week: dict[int, DayHours]) -> str:
    return json.dumps({
        str(i): {"status": d.status, "shifts": [[a.strftime("%H:%M"), b.strftime("%H:%M")] for a, b in d.shifts]}
        for i, d in week.items()
    }, ensure_ascii=False)


def from_json(raw: str) -> dict[int, DayHours]:
    data = json.loads(raw)
    return {int(i): DayHours(d["status"], [(time.fromisoformat(a), time.fromisoformat(b)) for a, b in d["shifts"]])
            for i, d in data.items()}


def _shift_bounds(day: date, start: time, end: time) -> tuple[datetime, datetime]:
    opens = datetime.combine(day, start, TEHRAN)
    closes = datetime.combine(day, end, TEHRAN)
    if closes <= opens:  # 10:00–00:00 or overnight: closes on the next day
        closes += timedelta(days=1)
    return opens, closes


def status_at(week: dict[int, DayHours], when: datetime) -> tuple[str, datetime | None]:
    """('open', closes_at) | ('closed', None) | ('unknown', None) at a Tehran datetime."""
    when = when.astimezone(TEHRAN)
    for offset in (0, -1):  # yesterday's overnight shift may still be running
        day = when.date() + timedelta(days=offset)
        dh = week.get(iran_weekday(day), DayHours("unknown"))
        if offset == 0 and dh.status == "unknown":
            return "unknown", None
        for start, end in dh.shifts:
            opens, closes = _shift_bounds(day, start, end)
            if opens <= when < closes:
                return "open", closes
    return "closed", None


def next_open(week: dict[int, DayHours], when: datetime, horizon_days: int = 7) -> datetime | None:
    """Earliest opening strictly after `when`; None if it can't be known."""
    when = when.astimezone(TEHRAN)
    for offset in range(horizon_days + 1):
        day = when.date() + timedelta(days=offset)
        dh = week.get(iran_weekday(day), DayHours("unknown"))
        if dh.status == "unknown":
            return None
        for start, end in sorted(dh.shifts):
            opens, _ = _shift_bounds(day, start, end)
            if opens > when:
                return opens
    return None


def open_at_arrival(week: dict[int, DayHours], arrival: datetime, buffer_min: int = 15) -> tuple[str, datetime | None]:
    """yes / tight / no / unknown, plus the closing time when open (spec §8)."""
    state, closes = status_at(week, arrival)
    if state == "unknown":
        return "unknown", None
    if state == "closed":
        return "no", None
    slack = (closes - arrival.astimezone(TEHRAN)).total_seconds() / 60
    return ("yes" if slack >= buffer_min else "tight"), closes

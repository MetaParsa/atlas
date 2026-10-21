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
            if _placeholder(start, end):
                continue  # 00:00–00:00 padding, or a 00:00:00–00:00:01 «shift» (58 shops' Fridays)
            shifts.append((start.replace(second=0, microsecond=0), end.replace(second=0, microsecond=0)))
        week[i] = DayHours("declared", _merge(shifts)) if status == "declared" and shifts else DayHours("unknown")
    return week


def _placeholder(start: time, end: time) -> bool:
    """A non-overnight shift shorter than 5 minutes is not real opening time (and must never become 24 h open)."""
    s, e = start.hour * 3600 + start.minute * 60 + start.second, end.hour * 3600 + end.minute * 60 + end.second
    return s == e or (e > s and e - s < 300) or (start == ZERO and end.hour == 0 and end.minute < 5)


def _merge(shifts: list[tuple[time, time]]) -> list[tuple[time, time]]:
    """Merge overlapping or back-to-back shifts (09–14 + 14–20 = 09–20) so «closes at» is the real closing time.
    A shift ending at 00:00 (midnight) or past midnight is kept as the day's last shift."""
    def end_key(e: time, s: time) -> tuple[int, int]:
        return (1, 0) if e <= s else (0, e.hour * 60 + e.minute)

    merged: list[tuple[time, time]] = []
    for s, e in sorted(shifts):
        if merged:
            ps, pe = merged[-1]
            if end_key(pe, ps)[0] == 0 and s <= pe:          # overlaps or touches the previous shift
                merged[-1] = (ps, e if end_key(e, s) > end_key(pe, ps) else pe)
                continue
        merged.append((s, e))
    return merged


def as_tehran(dt: datetime) -> datetime:
    """Naive datetimes (e.g. a DEMO_NOW typed without a zone) are Tehran time, never server-local time."""
    return dt.replace(tzinfo=TEHRAN) if dt.tzinfo is None else dt.astimezone(TEHRAN)


def hours_known(week: dict[int, DayHours]) -> bool:
    return any(d.status != "unknown" for d in week.values())


def to_json(week: dict[int, DayHours]) -> str:
    return json.dumps({
        str(i): {"status": d.status, "shifts": [[a.strftime("%H:%M"), b.strftime("%H:%M")] for a, b in d.shifts]}
        for i, d in week.items()
    }, ensure_ascii=False)


def from_json(raw: str) -> dict[int, DayHours]:
    data = json.loads(raw)
    week = {}
    for i, d in data.items():
        shifts = [(time.fromisoformat(a), time.fromisoformat(b)) for a, b in d["shifts"]]
        shifts = [(a, b) for a, b in shifts if not _placeholder(a, b)]   # defensive: an old DB must not mean 24 h open
        status = d["status"] if (shifts or d["status"] != "declared") else "unknown"
        week[int(i)] = DayHours(status, shifts)
    return week


def _shift_bounds(day: date, start: time, end: time) -> tuple[datetime, datetime]:
    opens = datetime.combine(day, start, TEHRAN)
    closes = datetime.combine(day, end, TEHRAN)
    if closes <= opens:  # 10:00–00:00 or overnight: closes on the next day
        closes += timedelta(days=1)
    return opens, closes


def status_at(week: dict[int, DayHours], when: datetime) -> tuple[str, datetime | None]:
    """('open', closes_at) | ('closed', None) | ('unknown', None) at a Tehran datetime."""
    when = as_tehran(when)
    today = week.get(iran_weekday(when.date()), DayHours("unknown"))
    for offset in (-1, 0):  # yesterday's overnight shift may still be running, even if today is unknown
        day = when.date() + timedelta(days=offset)
        for start, end in week.get(iran_weekday(day), DayHours("unknown")).shifts:
            opens, closes = _shift_bounds(day, start, end)
            if opens <= when < closes:
                return "open", closes
    return ("unknown", None) if today.status == "unknown" else ("closed", None)


def next_open(week: dict[int, DayHours], when: datetime, horizon_days: int = 7) -> datetime | None:
    """Earliest opening strictly after `when`; None if it can't be known."""
    when = as_tehran(when)
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
    slack = (closes - as_tehran(arrival)).total_seconds() / 60
    return ("yes" if slack >= buffer_min else "tight"), closes

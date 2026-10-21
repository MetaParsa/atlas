import json
from datetime import datetime

from torob.logic.hours import TEHRAN, from_json, iran_weekday, next_open, open_at_arrival, parse_weekly_hours, status_at, to_json


def week_json(thu=("09:00:00", "14:00:00"), fri_closed=True, sat=("10:00:00", "20:00:00")):
    shift = lambda a, b: {"shifts": [{"start_time": a, "end_time": b}, {"start_time": "00:00:00", "end_time": "00:00:00"}],
                          "status": "declared"}
    d = {k: shift(*sat) for k in ["sat", "sun", "mon", "tue", "wed"]}
    d["thu"] = shift(*thu)
    d["fri"] = {"shifts": [{"start_time": "00:00:00", "end_time": "00:00:00"}], "status": "closed"} if fri_closed else shift(*sat)
    return json.dumps(d)


THU = datetime(2026, 10, 22, 12, 0, tzinfo=TEHRAN)   # Thursday
FRI = datetime(2026, 10, 23, 11, 0, tzinfo=TEHRAN)


def test_iran_weekday():
    assert iran_weekday(THU.date()) == 5 and iran_weekday(FRI.date()) == 6


def test_padding_dropped_and_roundtrip():
    week = parse_weekly_hours(week_json())
    assert len(week[5].shifts) == 1
    assert from_json(to_json(week))[5].shifts == week[5].shifts


def test_thursday_short_day_and_friday_closed():
    week = parse_weekly_hours(week_json())
    assert status_at(week, THU)[0] == "open"
    assert status_at(week, THU.replace(hour=15))[0] == "closed"
    assert status_at(week, FRI)[0] == "closed"
    assert next_open(week, FRI) == datetime(2026, 10, 24, 10, 0, tzinfo=TEHRAN)  # Saturday 10:00


def test_open_at_arrival_tight_and_no():
    week = parse_weekly_hours(week_json())
    assert open_at_arrival(week, THU.replace(hour=13, minute=30))[0] == "yes"
    assert open_at_arrival(week, THU.replace(hour=13, minute=50))[0] == "tight"
    assert open_at_arrival(week, THU.replace(hour=14, minute=5))[0] == "no"


def test_unknown_is_never_guessed():
    week = parse_weekly_hours("{}")
    assert status_at(week, THU) == ("unknown", None)
    assert open_at_arrival(week, THU) == ("unknown", None)
    assert next_open(week, THU) is None


def test_shift_ending_at_midnight():
    week = parse_weekly_hours(week_json(thu=("18:00:00", "00:00:00")))
    state, closes = status_at(week, THU.replace(hour=23, minute=30))
    assert state == "open" and closes == datetime(2026, 10, 23, 0, 0, tzinfo=TEHRAN)

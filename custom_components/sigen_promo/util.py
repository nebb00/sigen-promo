"""Formatting promos for people and automations."""
from __future__ import annotations

from datetime import datetime, tzinfo
from typing import Any

from .const import REPEAT_NAMES, UNLABELLED, WEEKDAYS, WEEKLY
from .promo import next_occurrence, occurrences

DAY_NAMES = {v: k for k, v in WEEKDAYS.items()}



def weekday_names(p: dict[str, Any]) -> list[str]:
    if p["repeatType"] != WEEKLY or not p.get("weekRange"):
        return []
    return [DAY_NAMES.get(int(d), d) for d in str(p["weekRange"]).split(",")]


def money(value: float, currency: str = "") -> str:
    """A price per kWh in the station's currency, e.g. '£0.15/kWh', or '0.15' with no currency."""
    return f"{currency}{value:g}/kWh" if currency else f"{value:g}"


def price_text(p: dict[str, Any], currency: str = "") -> str:
    """e.g. 'Buy £0/kWh', 'Sell £1.5/kWh' or 'Buy £0.07/kWh · Sell £0.15/kWh'."""
    parts = []
    if p.get("buyPrice") is not None:
        parts.append(f"Buy {money(p['buyPrice'], currency)}")
    if p.get("sellPrice") is not None:
        parts.append(f"Sell {money(p['sellPrice'], currency)}")
    return " · ".join(parts)


def describe(p: dict[str, Any], label: str | None = None, currency: str = "") -> str:
    when = p["dt"]
    if p["repeatType"] != 0:
        days = ",".join(weekday_names(p)) or "daily"
        when += f" {days} to {p.get('endDate')}"
    text = f"{when} {p['timeRange']} {price_text(p, currency) or 'no prices'}"
    return f"{label}: {text}" if label else text


def readable(
    p: dict[str, Any], label: str | None, now: datetime, currency: str = ""
) -> dict[str, Any]:
    """A promo as plain fields for attributes and action responses."""
    nxt = next_occurrence(p, now)
    start, end = p["timeRange"].split("-")
    return {
        "label": label or UNLABELLED,
        "date": p["dt"],
        "start_time": start,
        "end_time": end,
        "repeat": REPEAT_NAMES.get(p["repeatType"], p["repeatType"]),
        "weekdays": weekday_names(p),
        "until": p.get("endDate"),
        "buy": p.get("buyPrice"),
        "sell": p.get("sellPrice"),
        "next_start": nxt[0].isoformat() if nxt else None,
        "next_end": nxt[1].isoformat() if nxt else None,
        "summary": describe(p, currency=currency),
    }


def windows(promos: list[dict[str, Any]], tz: tzinfo):
    """(now, active windows, next upcoming window); windows are (start, end, promo) tuples.

    The next window is the earliest one that starts after now, so while a daily promo
    is running its next window is tomorrow's.
    """
    now = datetime.now(tz)
    active, upcoming = [], []
    for p in promos:
        for s, e in occurrences(p, now):
            if s <= now:
                active.append((s, e, p))
            else:
                upcoming.append((s, e, p))
                break
    active.sort(key=lambda w: w[0])
    return now, active, min(upcoming, key=lambda w: w[0], default=None)

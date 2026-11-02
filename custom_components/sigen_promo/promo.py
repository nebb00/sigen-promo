"""Promo logic shared by the integration and the command-line tool.

No Home Assistant or network code here, so tools/sigen_promo.py can import it directly.
"""
from __future__ import annotations

import base64
from datetime import date, datetime, timedelta
from typing import Any

from .const import BOTH, BUY_ONLY, DAILY, ONCE, SELL_ONLY, WEEKLY


def encrypt_password(password: str) -> str:
    """AES-128-CBC with the app's fixed key and IV, base64-encoded."""
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key = b"sigensigensigenp"
    padder = padding.PKCS7(128).padder()
    data = padder.update(password.encode()) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(key)).encryptor()
    return base64.b64encode(enc.update(data) + enc.finalize()).decode()


def make_promo(
    day: date,
    start: str,
    end: str,
    buy: float | None,
    sell: float | None,
    repeat: int = ONCE,
    until: date | None = None,
    weekdays: list[int] | None = None,
) -> dict[str, Any]:
    """Build one promo in the shape the web app sends."""
    if buy is None and sell is None:
        raise ValueError("Give a buy price, a sell price, or both")
    kind = BOTH if buy is not None and sell is not None else BUY_ONLY if buy is not None else SELL_ONLY
    return {
        "dt": day.isoformat(),
        "endDate": until.isoformat() if repeat != ONCE and until else None,
        "weekRange": ",".join(map(str, sorted(weekdays))) if repeat == WEEKLY and weekdays else None,
        "repeatType": repeat,
        "type": kind,
        "timeRange": f"{start}-{end}",
        "buyPrice": buy,
        "sellPrice": sell,
    }


def normalise(p: dict[str, Any]) -> dict[str, Any]:
    """A promo as read, in the shape a save expects (unused price back to None)."""
    kind = p.get("type", BOTH)
    repeat = p.get("repeatType", ONCE)
    return {
        "dt": p["dt"],
        "endDate": p.get("endDate"),
        "weekRange": p.get("weekRange") if repeat == WEEKLY else None,
        "repeatType": repeat,
        "type": kind,
        "timeRange": p["timeRange"],
        "buyPrice": None if kind == SELL_ONLY else p.get("buyPrice"),
        "sellPrice": None if kind == BUY_ONLY else p.get("sellPrice"),
    }


def promo_key(p: dict[str, Any]) -> str:
    """Stable identity for a promo (the cloud has no ids): date, times, repeat and days."""
    return f"{p['dt']}|{p['timeRange']}|{p['repeatType']}|{p.get('weekRange') or ''}"


def first_start(p: dict[str, Any], tz) -> datetime:
    """When the promo's first window starts."""
    start = p["timeRange"].split("-")[0]
    return datetime.combine(date.fromisoformat(p["dt"]), datetime.min.time(), tz) + timedelta(
        minutes=minutes(start)
    )


def sort_key(p: dict[str, Any]):
    return (p["dt"], p["timeRange"], p["repeatType"], p.get("weekRange") or "")


def split_window(start: datetime, end: datetime) -> list[tuple[date, str, str]]:
    """Turn a local start/end into (day, HH:MM, HH:MM) pieces, splitting at midnight."""
    if end <= start:
        raise ValueError("The end must be after the start")
    pieces = []
    cur = start
    while cur < end:
        next_midnight = datetime.combine(cur.date() + timedelta(days=1), datetime.min.time(), cur.tzinfo)
        stop = min(end, next_midnight)
        stop_txt = "24:00" if stop == next_midnight else stop.strftime("%H:%M")
        if cur.strftime("%H:%M") != stop_txt:
            pieces.append((cur.date(), cur.strftime("%H:%M"), stop_txt))
        cur = stop
    return pieces


def minutes(t: str) -> int:
    """'HH:MM' to minutes past midnight ('24:00' is 1440)."""
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def _active(p: dict[str, Any]) -> tuple[date, date, set[int]]:
    start = date.fromisoformat(p["dt"])
    end = date.fromisoformat(p["endDate"]) if p["repeatType"] != ONCE and p.get("endDate") else start
    if p["repeatType"] == WEEKLY:
        days = {int(d) for d in str(p["weekRange"]).split(",")}
    elif p["repeatType"] == DAILY:
        days = set(range(1, 8))
    else:
        days = {start.isoweekday()}
    return start, end, days


def overlaps(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """True if two promos share at least one minute on at least one day."""
    a0, a1 = (minutes(t) for t in a["timeRange"].split("-"))
    b0, b1 = (minutes(t) for t in b["timeRange"].split("-"))
    if a1 <= b0 or b1 <= a0:
        return False
    as_, ae, ad = _active(a)
    bs, be, bd = _active(b)
    first, last = max(as_, bs), min(ae, be)
    if first > last:
        return False
    span = (last - first).days + 1
    return any((first + timedelta(days=i)).isoweekday() in ad & bd for i in range(min(span, 7)))


def same_promo(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return normalise(a) == normalise(b)


def occurrences(p: dict[str, Any], now: datetime):
    """The promo's windows (start, end) that haven't ended yet, in order, in now's timezone."""
    start_d, end_d, days = _active(p)
    t0, t1 = (minutes(t) for t in p["timeRange"].split("-"))
    d = max(start_d, now.date())
    while d <= end_d:
        if d.isoweekday() in days:
            midnight = datetime.combine(d, datetime.min.time(), now.tzinfo)
            s, e = midnight + timedelta(minutes=t0), midnight + timedelta(minutes=t1)
            if e > now:
                yield s, e
        d += timedelta(days=1)


def next_occurrence(p: dict[str, Any], now: datetime) -> tuple[datetime, datetime] | None:
    """Start and end of the promo's current or next window, in now's timezone."""
    return next(occurrences(p, now), None)

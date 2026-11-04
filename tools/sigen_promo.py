#!/usr/bin/env python3
"""Manage promotional tariffs (price overrides) on a Sigenergy station via the Sigen cloud.

A promo overrides the buy (import) and/or sell (export) price for a time window,
either once, daily or on chosen weekdays. It's the same feature as
Settings > Electricity price > Promotional in the Sigen app.

Commands:
  login     log in and save tokens (prompts for email and password)
  logout    log out and delete saved tokens
  station   print your station id
  list      show upcoming promos, numbered
  add       add a promo, or change the prices of one with the same date and times
  remove    remove promos by number (from `list`) or by date and start time
  clear     remove every promo

Run `sigen_promo.py <command> -h` for each command's options.
See tools/README.md for examples and what the API does behind the scenes.
"""
import argparse
import base64
import contextlib
import datetime as dt
import getpass
import json
import os
import re
import sys

import requests

# ---------------------------------------------------------------------------
# Configuration

CONFIG_DIR = os.path.expanduser(os.environ.get("SIGEN_CONFIG_DIR", "~/.config/sigen_promo"))
TOKEN_FILE = os.path.join(CONFIG_DIR, "tokens.json")
DEVICE_ID = "sigenpromo" + "0" * 23  # any stable 32-char id
TIMEOUT = 20

# Promo logic and API constants are shared with the Home Assistant integration.
import types  # noqa: E402
from pathlib import Path  # noqa: E402

_SHARED = Path(__file__).resolve().parent.parent / "custom_components" / "sigen_promo"
_pkg = types.ModuleType("sigen_shared")  # load const.py/promo.py without the HA-only __init__
_pkg.__path__ = [str(_SHARED)]
sys.modules["sigen_shared"] = _pkg
from sigen_shared.const import (  # noqa: E402,F401  # noqa: E402
    BOTH,
    BUY_ONLY,
    DAILY,
    DEFAULT_REGION,
    ONCE,
    REGIONS,
    SELL_ONLY,
    TESTED_REGIONS,
    WEEKLY,
    base_url,
)
from sigen_shared.const import WEEKDAYS as DAYS  # noqa: E402
from sigen_shared.promo import (  # noqa: E402
    encrypt_password,
    make_promo,
    minutes,
    normalise,
    overlaps,
    sort_key,
)

DAY_NAMES = {v: k for k, v in DAYS.items()}

# Set by set_region() before any request; Europe until then.
REGION = DEFAULT_REGION
BASE = PROMO_URL = ""
APP_HEADERS: dict = {}


def set_region(region: str):
    """Point every request at a region's Sigen cloud. SIGEN_BASE overrides the address."""
    global REGION, BASE, PROMO_URL, APP_HEADERS
    REGION = region
    BASE = os.environ.get("SIGEN_BASE") or base_url(region)
    PROMO_URL = f"{BASE}/electricity-price/api/promotional"
    # Headers the web app sends; the API may not need them all.
    APP_HEADERS = {
        "content-type": "application/json; charset=utf-8",
        "lang": "en_US",
        "client-server": region,
        "AUTH-CLIENT-ID": "sigen",
        "VERSION": "RELEASE",
        "sg-app-version": "4.1.0",
        "sg-v": "4.1.0",
        "sg-platform": "web",
        "sg-source": "flutterweb",
        "sg-pkg": "sigen_app",
        "Origin": f"https://app-{region}.sigencloud.com",
        "Referer": f"https://app-{region}.sigencloud.com/",
    }


set_region(DEFAULT_REGION)


def promo(day: str, start: str, end: str, buy: float | None, sell: float | None,
          repeat: int = ONCE, until: str | None = None, weekdays: str | None = None) -> dict:
    """make_promo() with the command line's string dates and "1,3,5" weekdays."""
    return make_promo(
        dt.date.fromisoformat(day), start, end, buy, sell, repeat,
        dt.date.fromisoformat(until) if until else None,
        [int(d) for d in weekdays.split(",")] if weekdays else None,
    )


class SigenError(Exception):
    pass


# ---------------------------------------------------------------------------
# Login and tokens

def _token_request(form: dict) -> dict:
    """POST to the OAuth endpoint and return the token data."""
    try:
        r = requests.post(
            f"{BASE}/auth/oauth/token",
            data={"scope": "server", **form},
            headers={"Authorization": "Basic " + base64.b64encode(b"sigen:sigen").decode()},
            timeout=TIMEOUT,
        )
    except requests.RequestException as e:
        raise SigenError(f"Can't reach Sigen cloud: {e}") from e
    try:
        body = r.json()
    except ValueError:
        body = {"msg": r.text[:200] or f"HTTP {r.status_code}"}
    data = body.get("data") if isinstance(body.get("data"), dict) else body
    if r.status_code == 200 and data.get("access_token"):
        return data
    raise SigenError(f"Login failed (HTTP {r.status_code}): {body.get('msg') or body}")


def login(user: str, password: str) -> dict:
    # The web app also sends signed device fields; the API works without them.
    return _token_request({
        "grant_type": "password",
        "userDeviceId": DEVICE_ID,
        "username": user,
        "password": encrypt_password(password),
    })


def refresh(refresh_token: str) -> dict | None:
    try:
        return _token_request({"grant_type": "refresh_token", "refresh_token": refresh_token})
    except SigenError:
        return None


def load_config() -> dict:
    try:
        with open(TOKEN_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict):
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f)


# ---------------------------------------------------------------------------
# API client

class Sigen:
    def __init__(self, token: str):
        self.s = requests.Session()
        self.s.headers.update(APP_HEADERS)
        self.s.headers["Authorization"] = f"bearer {token}"

    def _call(self, method: str, url: str, **kw):
        try:
            r = self.s.request(method, url, timeout=TIMEOUT, **kw)
        except requests.RequestException as e:
            raise SigenError(f"Can't reach Sigen cloud: {e}") from e
        try:
            body = r.json()
        except ValueError:
            raise SigenError(f"Unexpected reply (HTTP {r.status_code}): {r.text[:200]}") from None
        if r.status_code != 200 or body.get("code") != 0:
            raise SigenError(f"Sigen cloud refused it: {body.get('msg') or body}")
        return body.get("data")

    def valid(self) -> bool:
        try:
            self._call("GET", f"{BASE}/admin/user/me")
            return True
        except SigenError:
            return False

    def logout(self):
        with contextlib.suppress(SigenError):
            self._call("DELETE", f"{BASE}/auth/token/logout")

    def station_id(self) -> int:
        return int(self._call("GET", f"{BASE}/device/owner/station/home")["stationId"])

    def get_promos(self, station: int, since: dt.date) -> list:
        """Every promo still running on or after `since` (end date, or start date if none)."""
        return self._call("GET", PROMO_URL, params={"stationId": station, "dt": since.isoformat()}) or []

    def save_promos(self, station: int, promos: list):
        """Replace ALL promos on the station with `promos`. An empty list clears them."""
        return self._call("POST", PROMO_URL, json={"stationId": station, "promotionalList": promos})


def connect(args) -> tuple[Sigen, int]:
    """Get a working session (saved token, then refresh, then password) and the station id."""
    cfg = load_config()
    env_token = os.environ.get("SIGEN_TOKEN", "").removeprefix("bearer ").strip()
    token = env_token or cfg.get("access_token")
    api = Sigen(token) if token else None
    if not (api and api.valid()):
        new = refresh(cfg["refresh_token"]) if cfg.get("refresh_token") else None
        if not new:
            user, pw = os.environ.get("SIGEN_USER"), os.environ.get("SIGEN_PASS")
            if not (user and pw):
                raise SigenError("Not logged in. Run `sigen_promo.py login`, or set SIGEN_USER and SIGEN_PASS.")
            new = login(user, pw)
        # Keep the old refresh token if the reply doesn't include a new one.
        cfg.update(access_token=new["access_token"],
                   refresh_token=new.get("refresh_token") or cfg.get("refresh_token"))
        save_config(cfg)
        api = Sigen(new["access_token"])
    station = args.station or cfg.get("station_id")
    if not station:
        station = api.station_id()
        cfg["station_id"] = station
        save_config(cfg)
    return api, int(station)


# ---------------------------------------------------------------------------
# Promos

def fetch(api: Sigen, station: int) -> list:
    """All current and upcoming promos, sorted. Past ones can't be read, and saving drops them."""
    return sorted((normalise(p) for p in api.get_promos(station, dt.date.today())), key=sort_key)


def fmt_price(v) -> str:
    return "-" if v is None else f"{v:g}"


def describe(p: dict) -> str:
    when = p["dt"]
    if p["repeatType"] == DAILY:
        when += f" daily to {p['endDate']}"
    elif p["repeatType"] == WEEKLY:
        wd = ",".join(DAY_NAMES.get(int(d), d) for d in str(p["weekRange"]).split(","))
        when += f" {wd} to {p['endDate']}"
    return f"{when}  {p['timeRange']}  buy {fmt_price(p['buyPrice'])}  sell {fmt_price(p['sellPrice'])}"


def show(promos: list):
    if not promos:
        print("No promos.")
    for i, p in enumerate(promos, 1):
        print(f"{i:>3}  {describe(p)}")


# ---------------------------------------------------------------------------
# Command-line argument checks

def arg_date(s: str) -> str:
    try:
        return dt.date.fromisoformat(s).isoformat()
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{s}' isn't a date like 2026-11-05") from None


def arg_time(s: str) -> str:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s)
    if not m or int(m[2]) > 59 or int(m[1]) > 24 or (int(m[1]) == 24 and m[2] != "00"):
        raise argparse.ArgumentTypeError(f"'{s}' isn't a time like 16:30 (00:00 to 24:00)")
    return f"{int(m[1]):02d}:{m[2]}"


def arg_price(s: str) -> float:
    try:
        v = float(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{s}' isn't a number") from None
    if v < 0:
        raise argparse.ArgumentTypeError("Sigen doesn't accept negative prices")
    return v


def arg_days(s: str) -> str:
    try:
        nums = sorted({DAYS[d.strip().lower()[:3]] for d in s.split(",") if d.strip()})
    except KeyError:
        raise argparse.ArgumentTypeError(f"'{s}' isn't a list of days like mon,wed,fri") from None
    if not nums:
        raise argparse.ArgumentTypeError("give at least one day")
    return ",".join(map(str, nums))


def build_parser() -> argparse.ArgumentParser:
    fmt = argparse.RawDescriptionHelpFormatter
    region_help = (f"Sigen cloud region: {', '.join(REGIONS)} (default: the one you logged in with, "
                   f"else {DEFAULT_REGION}). Only {', '.join(sorted(TESTED_REGIONS))} is tested.")
    conn = argparse.ArgumentParser(add_help=False)
    conn.add_argument("--region", choices=REGIONS, help=region_help)
    conn.add_argument("--station", type=int, metavar="ID",
                      help="station id to use (default: your first station, saved after first lookup)")
    write = argparse.ArgumentParser(add_help=False)
    write.add_argument("-n", "--dry-run", action="store_true",
                       help="show the full list that would be sent, without saving")

    ap = argparse.ArgumentParser(prog="sigen_promo.py", description=__doc__, formatter_class=fmt)
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="command")

    lg = sub.add_parser("login", help="log in and save tokens", formatter_class=fmt, description=
        "Log in to the Sigen cloud and save the tokens, so later commands don't need your password.\n"
        "Prompts for email and password unless SIGEN_USER / SIGEN_PASS are set.\n"
        f"Tokens are saved to {TOKEN_FILE} (readable only by you) and renewed automatically.")
    lg.add_argument("--email", help="account email (default: SIGEN_USER, or prompt)")
    lg.add_argument("--region", choices=REGIONS, help=region_help)

    sub.add_parser("logout", help="log out and delete saved tokens",
                   description="Log out of the Sigen cloud and delete the saved tokens.")

    sub.add_parser("station", parents=[conn], help="print your station id",
                   description="Print the station id the other commands use.")

    ls = sub.add_parser("list", parents=[conn], help="show upcoming promos", formatter_class=fmt, description=
        "Show every current and upcoming promo, numbered. Use the numbers with `remove`.\n"
        "A '-' price means the promo doesn't change that price.")
    ls.add_argument("--json", action="store_true", help="print the promos as JSON, as the API returns them")

    a = sub.add_parser("add", parents=[conn, write], help="add or change a promo", formatter_class=fmt,
        description=
        "Add a promo. If one already exists with the same date and times, its prices and\n"
        "repeat are replaced instead. Give --buy, --sell or both. Prices are per kWh in\n"
        "the station's main currency unit (0.15, not 15, for 15p or 15c) and can't be negative.",
        epilog=
        "examples:\n"
        "  sigen_promo.py add 2026-11-05 03:00 04:00 --buy 0              free import, once\n"
        "  sigen_promo.py add 2026-11-05 16:00 19:00 --sell 0.25 --daily --until 2026-11-30\n"
        "  sigen_promo.py add 2026-11-07 00:00 24:00 --buy 0.07 --sell 0.15 --weekly sat,sun --until 2026-12-27")
    a.add_argument("date", type=arg_date, help="day of the promo, or first day if it repeats (YYYY-MM-DD)")
    a.add_argument("start", type=arg_time, help="start time, HH:MM")
    a.add_argument("end", type=arg_time, help="end time, HH:MM (24:00 for midnight)")
    a.add_argument("--buy", type=arg_price, metavar="PRICE", help="import price during the promo")
    a.add_argument("--sell", type=arg_price, metavar="PRICE", help="export price during the promo")
    rep = a.add_mutually_exclusive_group()
    rep.add_argument("--daily", action="store_true", help="repeat every day until --until")
    rep.add_argument("--weekly", type=arg_days, metavar="DAYS",
                     help="repeat on these days until --until, e.g. mon,wed,fri")
    a.add_argument("--until", type=arg_date, metavar="DATE",
                   help="last day of a repeating promo (YYYY-MM-DD, required with --daily/--weekly)")
    a.add_argument("--allow-overlap", action="store_true",
                   help="save even if it overlaps another promo (which one wins is unknown)")

    r = sub.add_parser("remove", parents=[conn, write], help="remove promos", formatter_class=fmt,
        description="Remove promos by their number from `list`, or by date and start time.",
        epilog="examples:\n  sigen_promo.py remove 2\n  sigen_promo.py remove 1 3\n"
               "  sigen_promo.py remove 2026-11-05 03:00")
    r.add_argument("which", nargs="+", metavar="N | DATE START",
                   help="list numbers, or a date and start time")

    c = sub.add_parser("clear", parents=[conn, write], help="remove every promo",
                       description="Remove every promo on the station.")
    c.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    return ap


def check_add(ap, args):
    if args.buy is None and args.sell is None:
        ap.error("add needs --buy, --sell or both")
    if (args.daily or args.weekly) and not args.until:
        ap.error("--daily and --weekly need --until (a repeating promo with no end date "
                 "vanishes from the cloud's list and would be wiped by the next save)")
    if args.until and not (args.daily or args.weekly):
        ap.error("--until only applies with --daily or --weekly")
    if args.date < dt.date.today().isoformat():
        ap.error("the date is in the past (the cloud silently drops those)")
    if args.until and args.until < args.date:
        ap.error("--until is before the start date")
    if minutes(args.end) <= minutes(args.start):
        ap.error("the end time must be after the start time")


# ---------------------------------------------------------------------------
# Commands

def save(api: Sigen, station: int, promos: list, dry_run: bool) -> list:
    promos = sorted(promos, key=sort_key)
    if dry_run:
        print("Dry run. This full list would be sent (it replaces all promos):")
        print(json.dumps({"stationId": station, "promotionalList": promos}, indent=2))
        return promos
    api.save_promos(station, promos)
    after = fetch(api, station)
    print("Saved. Promos now:")
    show(after)
    return after


def cmd_login(args):
    user = args.email or os.environ.get("SIGEN_USER") or input("Email: ").strip()
    pw = os.environ.get("SIGEN_PASS") or getpass.getpass("Password: ")
    tokens = login(user, pw)
    cfg = load_config()
    cfg.update(access_token=tokens["access_token"], refresh_token=tokens.get("refresh_token"), region=REGION)
    cfg.pop("station_id", None)
    save_config(cfg)
    station = Sigen(tokens["access_token"]).station_id()
    cfg["station_id"] = station
    save_config(cfg)
    print(f"Logged in ({REGION}). Station {station}. Tokens saved to {TOKEN_FILE}")


def cmd_logout(args):
    cfg = load_config()
    if cfg.get("access_token"):
        Sigen(cfg["access_token"]).logout()
    if os.path.exists(TOKEN_FILE):
        os.remove(TOKEN_FILE)
    print("Logged out.")


def cmd_station(args):
    print(connect(args)[1])


def cmd_list(args):
    api, station = connect(args)
    promos = fetch(api, station)
    if args.json:
        print(json.dumps(promos, indent=2))
    else:
        show(promos)


def cmd_add(args):
    api, station = connect(args)
    repeat = DAILY if args.daily else WEEKLY if args.weekly else ONCE
    new = promo(args.date, args.start, args.end, args.buy, args.sell, repeat, args.until, args.weekly)
    current = fetch(api, station)
    same = [p for p in current if p["dt"] == new["dt"] and p["timeRange"] == new["timeRange"]]
    others = [p for p in current if p not in same]
    clashes = [p for p in others if overlaps(p, new)]
    if clashes and not args.allow_overlap:
        msg = "\n".join(f"  {describe(p)}" for p in clashes)
        raise SigenError(f"That overlaps:\n{msg}\nRemove it first, or use --allow-overlap.")
    if same:
        print("Replacing:\n" + "\n".join(f"  {describe(p)}" for p in same))
    after = save(api, station, [*others, new], args.dry_run)
    if not args.dry_run and normalise(new) not in [normalise(p) for p in after]:
        raise SigenError("The cloud accepted the save but the new promo isn't in the list.")


def cmd_remove(args):
    api, station = connect(args)
    current = fetch(api, station)
    w = args.which
    if len(w) == 2 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", w[0]):
        start = arg_time(w[1])
        drop = [p for p in current if p["dt"] == w[0] and p["timeRange"].startswith(start + "-")]
        if len(drop) > 1:
            raise SigenError("More than one promo matches. Use the numbers from `list`.")
    else:
        try:
            nums = {int(x) for x in w}
        except ValueError:
            raise SigenError("Give list numbers (e.g. `remove 2`) or a date and start time.") from None
        bad = [n for n in nums if not 1 <= n <= len(current)]
        if bad:
            raise SigenError(f"No promo numbered {', '.join(map(str, sorted(bad)))}. Run `list` to see them.")
        drop = [current[n - 1] for n in sorted(nums)]
    if not drop:
        raise SigenError("No matching promo found.")
    print("Removing:\n" + "\n".join(f"  {describe(p)}" for p in drop))
    save(api, station, [p for p in current if p not in drop], args.dry_run)


def cmd_clear(args):
    api, station = connect(args)
    current = fetch(api, station)
    if not current:
        print("No promos.")
        return
    show(current)
    if not (args.yes or args.dry_run) and input(
        f"Remove all {len(current)}? [y/N] "
    ).strip().lower() not in ("y", "yes"):
            print("Nothing changed.")
            return
    save(api, station, [], args.dry_run)


def main():
    ap = build_parser()
    args = ap.parse_args()
    set_region(getattr(args, "region", None) or os.environ.get("SIGEN_REGION")
               or load_config().get("region") or DEFAULT_REGION)
    if args.cmd == "add":
        check_add(ap, args)
    try:
        globals()[f"cmd_{args.cmd}"](args)
    except SigenError as e:
        sys.exit(f"Error: {e}")
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()

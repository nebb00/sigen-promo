# sigen_promo

Command-line tool for promotional tariffs on a Sigenergy station: one-off or repeating
price overrides for import (buy), export (sell) or both. It does the same as the
Promotional price screen in the Sigen app, through the same cloud API.

Built to test the API ahead of a Home Assistant integration (for things like Axle VPP
events and EDF free-energy slots).

## Setup

The tool uses the integration's promo code in `custom_components/sigen_promo/`, so keep
it inside a full copy of the repo.


```bash
pip install requests cryptography
python3 tools/sigen_promo.py login
```

`login` asks for your Sigen email and password. It saves the login tokens to
`~/.config/sigen_promo/tokens.json` (only you can read it) and renews them
automatically, so you only log in once.

## Commands

| Command | What it does |
|---|---|
| `login` | Log in and save tokens |
| `logout` | Log out and delete saved tokens |
| `station` | Print your station id |
| `list` | Show current and upcoming promos, numbered |
| `add` | Add a promo, or change one with the same date and times |
| `remove` | Remove promos by list number, or by date and start time |
| `clear` | Remove every promo |

Every command has its own help: `python3 tools/sigen_promo.py add -h`.

### `add DATE START END [options]`

| Argument / option | Meaning |
|---|---|
| `DATE` | Day of the promo, or the first day if it repeats (`YYYY-MM-DD`). Can't be in the past. |
| `START`, `END` | Times as `HH:MM`. Any minute works; use `24:00` for midnight at the end of the day. |
| `--buy PRICE` | Import price during the promo. |
| `--sell PRICE` | Export price during the promo. |
| `--daily` | Repeat every day until `--until`. |
| `--weekly DAYS` | Repeat on these days until `--until`, e.g. `mon,wed,fri` or `sat,sun`. |
| `--until DATE` | Last day of a repeating promo. Required with `--daily` / `--weekly`. |
| `--allow-overlap` | Save even if the promo overlaps another one. |
| `-n`, `--dry-run` | Show what would be sent, without saving. |
| `--station ID` | Use a different station (see below). |

Give `--buy`, `--sell` or both. Leaving one out means the promo doesn't touch that
price. Prices are per kWh in your station's main currency unit, as the API stores them: `0.15`
means 0.15 of your currency (15p, 15c), not 15. They can't be negative.

If a promo with the same date and start/end times already exists, `add` replaces it,
which is how you change a promo's prices.

```bash
# Free import 3am-4am on 5 Nov
python3 tools/sigen_promo.py add 2026-11-05 03:00 04:00 --buy 0

# Export at 25p, 4pm-7pm every day for the rest of November
python3 tools/sigen_promo.py add 2026-11-05 16:00 19:00 --sell 0.25 --daily --until 2026-11-30

# Weekends all day: import 7p, export 15p, until the end of the year
python3 tools/sigen_promo.py add 2026-11-07 00:00 24:00 --buy 0.07 --sell 0.15 --weekly sat,sun --until 2026-12-27
```

### `list [--json]`

```
  1  2026-11-05  03:00-04:00  buy 0  sell -
  2  2026-11-06 daily to 2026-11-10  03:00-04:00  buy -  sell 25
  3  2026-11-14 sat,sun to 2026-12-27  00:00-24:00  buy 7  sell 15
```

`-` means the promo doesn't change that price. `--json` prints the promos as the API
stores them, for use in other scripts.

### `remove N... | remove DATE START`

```bash
python3 tools/sigen_promo.py remove 2              # number from `list`
python3 tools/sigen_promo.py remove 1 3            # several at once
python3 tools/sigen_promo.py remove 2026-11-05 03:00
```

Also takes `-n` / `--dry-run` and `--station`.

### `clear [-y]`

Removes every promo. Asks first unless you pass `-y` / `--yes`. Also takes `--dry-run`
and `--station`.

### Options for all commands except `logout`

| Option | Meaning |
|---|---|
| `--region REGION` | Sigen cloud region: `eu`, `us`, `apac`, `aus` or `cn`. `login` saves it, so you only need it there. Only `eu` has been tested. |
| `--station ID` | Station to use (not on `login`). By default it's looked up once after login and saved. |

## Environment variables

| Variable | Use |
|---|---|
| `SIGEN_USER`, `SIGEN_PASS` | Log in without prompts (for scripts or cron). Used only when the saved tokens don't work. |
| `SIGEN_TOKEN` | Use this access token (the `bearer ...` value from browser dev tools) instead of the saved one. |
| `SIGEN_REGION` | Sigen cloud region: `eu` (default), `us`, `apac`, `aus` or `cn`. Same as `--region`. Only `eu` has been tested. |
| `SIGEN_BASE` | Override the API address entirely (for testing). |
| `SIGEN_CONFIG_DIR` | Where tokens are saved. Default `~/.config/sigen_promo`. |

## How the API behaves

All tested against a live account and browser captures on 2026-11-01.

- **Read:** `GET /electricity-price/api/promotional?stationId=ID&dt=YYYY-MM-DD` returns
  every promo whose end date (or start date, if it has no end date) is on or after `dt`.
- **Save:** `POST /electricity-price/api/promotional` with
  `{"stationId": ID, "promotionalList": [...]}` **replaces every promo on the station**.
  An empty list clears them all. So the script always reads first, makes its change and
  sends the whole list back.
- **Promo fields:**
  - `type`: `1` buy only, `2` sell only, `3` both. The unused price is sent as `null`
    and read back as `0.0`.
  - `repeatType`: `0` once, `1` daily, `2` weekly.
  - `weekRange`: weekly days as `"1,3,5"`, with `1` = Monday and `7` = Sunday. For daily
    promos the server fills in `"1,2,3,4,5,6,7"`.
  - `endDate`: last day of a repeating promo.
  - `timeRange`: `"HH:MM-HH:MM"`, end after start, `24:00` allowed.
- **Validation:** the server rejects negative prices, an end before the start, and a
  missing price for the type. It accepts overlapping promos (which one applies is
  unknown, so the script refuses them unless you pass `--allow-overlap`). It accepts
  past-dated promos but then drops them.
- **Repeating promos without an end date** stop being returned by reads the day after
  they start, so the next save would wipe them. The script always requires an end date.
- **Login:** `POST /auth/oauth/token` (form data, basic auth `sigen:sigen`) with
  `grant_type=password`, your email, and your password AES-128-CBC encrypted with
  key and IV `sigensigensigenp`, base64-encoded. The reply includes an access token and
  a refresh token; `grant_type=refresh_token` renews them. The web app also sends signed
  device fields, but the API works without them. Logout is `DELETE /auth/token/logout`.

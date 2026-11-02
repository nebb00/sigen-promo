"""Constants for the Sigen promotional tariffs integration."""

DOMAIN = "sigen_promo"

CONF_STATION_ID = "station_id"
CONF_SCAN_INTERVAL = "scan_interval"

CONF_REGION = "region"

# Sigen cloud regions, from the open-source Sigen libraries. Only "eu" has been tested
# with promos; the feature may not exist elsewhere.
REGIONS = ["eu", "us", "apac", "aus", "cn"]
TESTED_REGIONS = {"eu"}
DEFAULT_REGION = "eu"


def base_url(region: str) -> str:
    return f"https://api-{region}.sigencloud.com"


DEFAULT_SCAN_MINUTES = 15
MIN_SCAN_MINUTES = 5
MAX_SCAN_MINUTES = 60

# Saves that fail because the cloud is unreachable are retried after these waits (seconds).
SAVE_RETRY_DELAYS = (5, 20, 60)

# Shown for promos that weren't added by this integration (e.g. set in the Sigen app).
UNLABELLED = "Sigen app"

# API values, confirmed against the live API on 2026-11-01.
# type: which prices the promo sets.
BUY_ONLY, SELL_ONLY, BOTH = 1, 2, 3
# repeatType: 0 = once, 1 = daily, 2 = weekly on weekRange days ("1,3,5", 1 = Monday).
ONCE, DAILY, WEEKLY = 0, 1, 2
WEEKDAYS = {"mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6, "sun": 7}
REPEAT_NAMES = {ONCE: "once", DAILY: "daily", WEEKLY: "weekly"}

# What add_promo does when the new promo overlaps an existing one.
OVERLAP_ERROR = "error"
OVERLAP_REPLACE = "replace"
OVERLAP_ALLOW = "allow"

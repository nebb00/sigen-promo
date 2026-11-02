"""Sigen promotional tariffs: set one-off or repeating price overrides on a Sigenergy station."""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, tzinfo
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import SigenAuthError, SigenClient, SigenError, SigenRefused
from .const import (
    CONF_REGION,
    CONF_SCAN_INTERVAL,
    CONF_STATION_ID,
    DAILY,
    DEFAULT_REGION,
    DEFAULT_SCAN_MINUTES,
    DOMAIN,
    ONCE,
    OVERLAP_ALLOW,
    OVERLAP_ERROR,
    OVERLAP_REPLACE,
    SAVE_RETRY_DELAYS,
    WEEKDAYS,
    WEEKLY,
)
from .promo import (
    first_start,
    make_promo,
    overlaps,
    promo_key,
    same_promo,
    split_window,
)
from .util import describe, readable

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

ATTR_ENTRY = "config_entry_id"
PRICE = vol.All(vol.Coerce(float), vol.Range(min=0))
LABEL = vol.All(cv.string, vol.Length(min=1, max=60))

ADD_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ENTRY): cv.string,
        vol.Required("start"): cv.datetime,
        vol.Required("end"): cv.datetime,
        vol.Optional("buy"): vol.Any(None, PRICE),
        vol.Optional("sell"): vol.Any(None, PRICE),
        vol.Optional("label"): LABEL,
        vol.Optional("replace_label", default=False): cv.boolean,
        vol.Optional("repeat", default="once"): vol.In(["once", "daily", "weekly"]),
        vol.Optional("weekdays"): vol.All(cv.ensure_list, [vol.In(list(WEEKDAYS))]),
        vol.Optional("until"): cv.date,
        vol.Optional("on_overlap", default=OVERLAP_ERROR): vol.In(
            [OVERLAP_ERROR, OVERLAP_REPLACE, OVERLAP_ALLOW]
        ),
    }
)
REMOVE_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Optional(ATTR_ENTRY): cv.string,
            vol.Inclusive("start", "window", msg="give both start and end"): cv.datetime,
            vol.Inclusive("end", "window", msg="give both start and end"): cv.datetime,
            vol.Optional("label"): LABEL,
            vol.Optional("include_started", default=False): cv.boolean,
        }
    ),
    cv.has_at_least_one_key("label", "start"),
)
ENTRY_ONLY_SCHEMA = vol.Schema({vol.Optional(ATTR_ENTRY): cv.string})

type SigenConfigEntry = ConfigEntry[SigenPromoCoordinator]


class SigenPromoCoordinator(DataUpdateCoordinator[list[dict[str, Any]]]):
    """Holds the station's current and upcoming promos, and the labels HA gave them."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: SigenClient, tz: tzinfo) -> None:
        minutes = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_MINUTES)
        super().__init__(
            hass, _LOGGER, config_entry=entry, name=DOMAIN, update_interval=timedelta(minutes=minutes)
        )
        self.client = client
        self.station: int = entry.data[CONF_STATION_ID]
        self.tz = tz
        # Sigen promos have no name field, so labels live here, keyed by promo_key().
        self._store: Store[dict[str, str]] = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.labels")
        self.labels: dict[str, str] = {}

    @property
    def _issue_id(self) -> str:
        return f"save_failed_{self.config_entry.entry_id}"

    def _report_failed(self, error: str) -> None:
        """Raise a Repairs issue: a change didn't reach the Sigen cloud."""
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="save_failed",
            translation_placeholders={"station": self.config_entry.title, "error": error},
        )

    def clear_failed(self) -> None:
        """The last change went through: remove the Repairs issue, if any."""
        ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def label_of(self, p: dict[str, Any]) -> str | None:
        return self.labels.get(promo_key(p))

    def readable(self, p: dict[str, Any]) -> dict[str, Any]:
        return readable(p, self.label_of(p), self.now())

    async def async_load_labels(self) -> None:
        self.labels = await self._store.async_load() or {}

    async def _sync_labels(self, promos: list[dict[str, Any]]) -> None:
        """Forget labels of promos that are gone (ended, removed, or changed in the app)."""
        keys = {promo_key(p) for p in promos}
        kept = {k: v for k, v in self.labels.items() if k in keys}
        if kept != self.labels:
            self.labels = kept
            await self._store.async_save(self.labels)

    async def _async_update_data(self) -> list[dict[str, Any]]:
        try:
            promos = await self.client.get_promos(self.station, self.now().date())
        except SigenAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except SigenError as err:
            raise UpdateFailed(str(err)) from err
        await self._sync_labels(promos)
        return promos

    async def change(self, edit, new_labels: dict[str, str] | None = None) -> list[dict[str, Any]]:
        """Read the latest promos, apply `edit(current) -> new list`, save, and refresh.

        A save replaces every promo on the station, so this always works from a fresh
        read and runs one change at a time. If the cloud can't be reached the whole
        read-edit-save is retried a few times before giving up.
        """
        async with self.client.write_lock:
            for attempt, wait in enumerate((0, *SAVE_RETRY_DELAYS)):
                if wait:
                    _LOGGER.warning("Sigen cloud unavailable, retrying in %s s (attempt %s)", wait, attempt + 1)
                    await asyncio.sleep(wait)
                try:
                    current = await self.client.get_promos(self.station, self.now().date())
                    new = edit(current)
                    if new is None or new == current:
                        after = current  # nothing to change: no save, no re-read
                    else:
                        await self.client.save_promos(self.station, new)
                        after = await self.client.get_promos(self.station, self.now().date())
                    break
                except SigenAuthError as err:
                    self.config_entry.async_start_reauth(self.hass)
                    raise HomeAssistantError(f"Sigen login failed: {err}") from err
                except SigenError as err:
                    if attempt == len(SAVE_RETRY_DELAYS) or isinstance(err, SigenRefused):
                        self._report_failed(str(err))
                        raise HomeAssistantError(str(err)) from err
        self.clear_failed()
        if new_labels:
            self.labels.update(new_labels)
        await self._sync_labels(after)
        await self._store.async_save(self.labels)
        self.async_set_updated_data(after)
        return after


def _started(p: dict[str, Any], coord: SigenPromoCoordinator) -> bool:
    return first_start(p, coord.tz) <= coord.now()


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the actions once, for all entries."""

    def coordinator_for(call: ServiceCall) -> SigenPromoCoordinator:
        entries = [
            e for e in hass.config_entries.async_loaded_entries(DOMAIN)
            if ATTR_ENTRY not in call.data or e.entry_id == call.data[ATTR_ENTRY]
        ]
        if not entries:
            raise HomeAssistantError("No loaded Sigen promotional tariffs entry found")
        if len(entries) > 1:
            raise HomeAssistantError("More than one Sigen station: set config_entry_id")
        return entries[0].runtime_data

    def local(value: datetime, coord: SigenPromoCoordinator) -> datetime:
        """In the station's timezone, to the minute. Naive times are taken as HA local time."""
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt_util.get_default_time_zone())
        return value.astimezone(coord.tz).replace(second=0, microsecond=0)

    async def add_promo(call: ServiceCall) -> ServiceResponse:
        coord = coordinator_for(call)
        data = call.data
        buy, sell = data.get("buy"), data.get("sell")
        label = data.get("label")
        if buy is None and sell is None:
            raise HomeAssistantError("Give buy, sell or both")
        if data["replace_label"] and not label:
            raise HomeAssistantError("replace_label needs a label")
        start, end = local(data["start"], coord), local(data["end"], coord)
        now = coord.now()
        if end <= now:
            raise HomeAssistantError("That window has already ended")
        repeat = {"once": ONCE, "daily": DAILY, "weekly": WEEKLY}[data["repeat"]]
        # The cloud drops promos dated before today. A one-off window that's already
        # running is saved from midnight today; a repeating one keeps its daily times
        # and just starts today.
        today = now.replace(hour=0, minute=0)
        if start < today:
            if repeat == ONCE:
                start = today
            else:
                start = start.replace(year=today.year, month=today.month, day=today.day)
                end = start.replace(hour=end.hour, minute=end.minute)
                if end <= start:  # e.g. an end of 00:00 meaning midnight
                    end += timedelta(days=1)
        until: date | None = data.get("until")
        weekdays = [WEEKDAYS[d] for d in data.get("weekdays", [])]
        if repeat != ONCE and until is None:
            raise HomeAssistantError(
                "Repeating promos need 'until' (without an end date the cloud stops listing "
                "them after the first day, and the next change would wipe them)"
            )
        if repeat == WEEKLY and not weekdays:
            raise HomeAssistantError("Weekly promos need 'weekdays'")
        try:
            pieces = split_window(start, end)
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err
        if repeat != ONCE and len(pieces) > 1:
            raise HomeAssistantError("A repeating promo can't run past midnight")
        if until is not None and until < start.date():
            raise HomeAssistantError("'until' is before the start date")
        new = [make_promo(d, s, e, buy, sell, repeat, until, weekdays) for d, s, e in pieces]
        mode = data["on_overlap"]
        replaced: list[dict[str, Any]] = []

        def edit(current: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
            replaced.clear()
            keep = list(current)
            if data["replace_label"]:
                # This label stands for one thing (e.g. "the next Axle event"): drop its
                # older promos that haven't started yet, unless they're identical to the new ones.
                stale = [
                    c for c in keep
                    if coord.label_of(c) == label and not _started(c, coord)
                    and not any(same_promo(c, p) for p in new)
                ]
                replaced.extend(stale)
                keep = [c for c in keep if c not in stale]
            todo = [p for p in new if not any(same_promo(p, c) for c in keep)]
            clashes = [c for c in keep if any(overlaps(c, p) for p in todo)]
            if clashes and mode == OVERLAP_ERROR:
                raise HomeAssistantError(
                    "Overlaps existing promo(s): "
                    + "; ".join(describe(c, coord.label_of(c)) for c in clashes)
                    + ". Remove them first, or set on_overlap to replace or allow."
                )
            if mode == OVERLAP_REPLACE:
                replaced.extend(clashes)
                keep = [c for c in keep if c not in clashes]
            if not todo and not replaced:
                return None  # already there: running the same automation twice is harmless
            return keep + todo

        labels = {promo_key(p): label for p in new} if label else None
        before = dict(coord.labels)
        after = await coord.change(edit, labels)
        return {
            "added": [coord.readable(p) for p in new],
            "removed": [readable(p, before.get(promo_key(p)), coord.now()) for p in replaced],
            "promos": [coord.readable(p) for p in after],
        }

    async def remove_promo(call: ServiceCall) -> ServiceResponse:
        coord = coordinator_for(call)
        data = call.data
        window = []
        if "start" in data:
            try:
                pieces = split_window(local(data["start"], coord), local(data["end"], coord))
            except ValueError as err:
                raise HomeAssistantError(str(err)) from err
            window = [make_promo(d, s, e, 0, None) for d, s, e in pieces]
        label = data.get("label")
        removed: list[dict[str, Any]] = []

        def edit(current):
            removed.clear()
            for c in current:
                if label and coord.label_of(c) != label:
                    continue
                if window and not any(overlaps(c, w) for w in window):
                    continue
                if not data["include_started"] and _started(c, coord):
                    continue
                removed.append(c)
            return [c for c in current if c not in removed] if removed else None

        before = dict(coord.labels)
        after = await coord.change(edit)
        return {
            "removed": [readable(p, before.get(promo_key(p)), coord.now()) for p in removed],
            "promos": [coord.readable(p) for p in after],
        }

    async def clear_promos(call: ServiceCall) -> ServiceResponse:
        coord = coordinator_for(call)
        removed: list[dict[str, Any]] = []

        def edit(current):
            removed[:] = current
            return [] if current else None

        before = dict(coord.labels)
        await coord.change(edit)
        return {
            "removed": [readable(p, before.get(promo_key(p)), coord.now()) for p in removed],
            "promos": [],
        }

    async def list_promos(call: ServiceCall) -> ServiceResponse:
        coord = coordinator_for(call)
        await coord.async_refresh()
        if not coord.last_update_success:
            raise HomeAssistantError(f"Couldn't read promos from the Sigen cloud: {coord.last_exception}")
        return {"promos": [coord.readable(p) for p in coord.data or []]}

    optional = SupportsResponse.OPTIONAL
    hass.services.async_register(DOMAIN, "add_promo", add_promo, ADD_SCHEMA, supports_response=optional)
    hass.services.async_register(DOMAIN, "remove_promo", remove_promo, REMOVE_SCHEMA, supports_response=optional)
    hass.services.async_register(DOMAIN, "clear_promos", clear_promos, ENTRY_ONLY_SCHEMA, supports_response=optional)
    hass.services.async_register(
        DOMAIN, "list_promos", list_promos, ENTRY_ONLY_SCHEMA, supports_response=SupportsResponse.ONLY
    )
    return True


def _client(hass: HomeAssistant, entry: ConfigEntry, on_tokens=None) -> SigenClient:
    return SigenClient(
        async_get_clientsession(hass),
        entry.data.get(CONF_REGION, DEFAULT_REGION),
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
        tokens={k: entry.data.get(k) for k in ("access_token", "refresh_token", "expires_at")},
        on_tokens=on_tokens,
    )


async def async_setup_entry(hass: HomeAssistant, entry: SigenConfigEntry) -> bool:
    async def save_tokens(tokens: dict[str, Any]) -> None:
        hass.config_entries.async_update_entry(entry, data={**entry.data, **tokens})

    client = _client(hass, entry, save_tokens)
    # Promo times are in the station's local time; fall back to HA's timezone.
    tz = dt_util.get_default_time_zone()
    try:
        info = await client.station_info(entry.data[CONF_STATION_ID])
        tz = dt_util.get_time_zone(info.get("timeZoneName") or "") or tz
    except SigenAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except SigenError as err:
        _LOGGER.debug("Couldn't read station timezone, using HA's: %s", err)

    coordinator = SigenPromoCoordinator(hass, entry, client, tz)
    await coordinator.async_load_labels()
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(_options_updated))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _options_updated(hass: HomeAssistant, entry: SigenConfigEntry) -> None:
    coord = entry.runtime_data
    minutes = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_MINUTES)
    if coord.update_interval != timedelta(minutes=minutes):
        coord.update_interval = timedelta(minutes=minutes)
        await coord.async_request_refresh()


async def async_unload_entry(hass: HomeAssistant, entry: SigenConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Log out of the Sigen cloud and drop the saved labels when the integration is deleted."""
    await _client(hass, entry).logout()
    await Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.labels").async_remove()
    ir.async_delete_issue(hass, DOMAIN, f"save_failed_{entry.entry_id}")

"""Diagnostics download, with the login details removed."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from . import SigenConfigEntry

TO_REDACT = {CONF_USERNAME, CONF_PASSWORD, "access_token", "refresh_token"}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: SigenConfigEntry) -> dict[str, Any]:
    coord = entry.runtime_data
    return {
        "entry": {"data": async_redact_data(dict(entry.data), TO_REDACT), "options": dict(entry.options)},
        "region": entry.data.get("region", "eu"),
        "station_timezone": str(coord.tz),
        "currency": coord.currency,
        "update_interval": str(coord.update_interval),
        "last_update_success": coord.last_update_success,
        "promos": coord.data,
        "labels": coord.labels,
    }

"""Setup: log in with the Sigen app email and password."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import SigenAuthError, SigenClient, SigenError, SigenRefused
from .const import (
    CONF_REGION,
    CONF_SCAN_INTERVAL,
    CONF_STATION_ID,
    DEFAULT_REGION,
    DEFAULT_SCAN_MINUTES,
    DOMAIN,
    MAX_SCAN_MINUTES,
    MIN_SCAN_MINUTES,
    REGIONS,
)

REGION_SELECTOR = selector.SelectSelector(
    selector.SelectSelectorConfig(
        options=REGIONS, translation_key="region", mode=selector.SelectSelectorMode.DROPDOWN
    )
)
SCHEMA = vol.Schema(
    {
        vol.Required(CONF_REGION, default=DEFAULT_REGION): REGION_SELECTOR,
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)
# Only on first setup: leave empty for the account's main station.
USER_SCHEMA = SCHEMA.extend({vol.Optional(CONF_STATION_ID): str})


def _title(info: dict[str, Any]) -> str:
    name = info.get("stationShowName") or info.get("stationName") or "Sigen station"
    return f"{name} ({info['stationId']})"


class SigenPromoConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1
    _station_info: dict[str, Any]

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return SigenPromoOptionsFlow()

    async def _try_login(
        self, region: str, user: str, password: str, station: int | None = None
    ) -> tuple[dict[str, Any] | None, dict[str, str]]:
        """Log in and read the station (the account's main one if no id is given)."""
        client = SigenClient(async_get_clientsession(self.hass), region, user, password)
        try:
            await client.login()
        except SigenAuthError:
            return None, {"base": "invalid_auth"}
        except SigenError:
            return None, {"base": "cannot_connect"}
        try:
            info = await client.station_info(station)
        except SigenRefused:
            return None, {CONF_STATION_ID: "invalid_station"}
        except SigenError:
            return None, {"base": "cannot_connect"}
        self._station_info = info
        return {
            CONF_REGION: region,
            CONF_USERNAME: user,
            CONF_PASSWORD: password,
            CONF_STATION_ID: int(info["stationId"]),
            **client.tokens,
        }, {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            station_txt = (user_input.get(CONF_STATION_ID) or "").strip()
            if station_txt and not station_txt.isdigit():
                errors[CONF_STATION_ID] = "invalid_station"
            else:
                data, errors = await self._try_login(
                    user_input[CONF_REGION],
                    user_input[CONF_USERNAME],
                    user_input[CONF_PASSWORD],
                    int(station_txt) if station_txt else None,
                )
                if data:
                    await self.async_set_unique_id(str(data[CONF_STATION_ID]))
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(title=_title(self._station_info), data=data)
        return self.async_show_form(
            step_id="user", data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, user_input), errors=errors
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """The saved login stopped working: ask for the password again."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = await self._try_login(
                entry.data.get(CONF_REGION, DEFAULT_REGION),
                entry.data[CONF_USERNAME],
                user_input[CONF_PASSWORD],
                entry.data[CONF_STATION_ID],
            )
            if errors.get(CONF_STATION_ID):
                errors = {"base": "wrong_station"}
            if data:
                await self.async_set_unique_id(str(data[CONF_STATION_ID]))
                self._abort_if_unique_id_mismatch(reason="wrong_station")
                return self.async_update_reload_and_abort(entry, data={**entry.data, **data})
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            description_placeholders={"username": entry.data[CONF_USERNAME]},
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Change the email and/or password. The login must belong to the same station."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = await self._try_login(
                user_input[CONF_REGION],
                user_input[CONF_USERNAME],
                user_input[CONF_PASSWORD],
                entry.data[CONF_STATION_ID],
            )
            if errors.get(CONF_STATION_ID):  # the new login can't see this station
                errors = {"base": "wrong_station"}
            if data:
                await self.async_set_unique_id(str(data[CONF_STATION_ID]))
                self._abort_if_unique_id_mismatch(reason="wrong_station")
                return self.async_update_reload_and_abort(entry, data={**entry.data, **data})
        current = user_input or entry.data
        suggested = {
            CONF_REGION: current.get(CONF_REGION, DEFAULT_REGION),
            CONF_USERNAME: current[CONF_USERNAME],
        }
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(SCHEMA, suggested),
            errors=errors,
        )


class SigenPromoOptionsFlow(OptionsFlow):
    """How often to re-read promos from the cloud."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data={CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL])})
        current = self.config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_MINUTES)
        schema = vol.Schema(
            {
                vol.Required(CONF_SCAN_INTERVAL, default=current): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=MIN_SCAN_MINUTES, max=MAX_SCAN_MINUTES, step=1,
                        unit_of_measurement="min", mode=selector.NumberSelectorMode.BOX,
                    )
                )
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)

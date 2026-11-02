"""Sensors: the promo list, the promo running now, and when the next one starts."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_change

from . import SigenConfigEntry
from .const import UNLABELLED
from .entity import SigenPromoEntity
from .util import price_text, windows

NONE_RUNNING = "None"


async def async_setup_entry(
    hass: HomeAssistant, entry: SigenConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coord = entry.runtime_data
    async_add_entities([PromoCountSensor(coord), CurrentPromoSensor(coord), NextPromoSensor(coord)])


class _MinuteTick(SigenPromoEntity):
    """Promos start and end on the minute; re-check every minute, not just on polls."""

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        @callback
        def tick(_now) -> None:
            self.async_write_ha_state()

        self.async_on_remove(async_track_time_change(self.hass, tick, second=0))


class PromoCountSensor(SigenPromoEntity, SensorEntity):
    """How many current and upcoming promos there are; the list is in `promos`."""

    _attr_translation_key = "promos"
    _attr_native_unit_of_measurement = "promos"
    # The full list can be long and is always available live; keep it out of history.
    _unrecorded_attributes = frozenset({"promos"})

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "promos")

    @property
    def native_value(self) -> int:
        return len(self.coordinator.data or [])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"promos": [self.coordinator.readable(p) for p in self.coordinator.data or []]}


class CurrentPromoSensor(_MinuteTick, SensorEntity):
    """The promo running now, e.g. 'Axle event · Sell 100', or 'None'."""

    _attr_translation_key = "current_promo"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "current_promo")

    def _active(self):
        _, active, _ = windows(self.coordinator.data or [], self.coordinator.tz)
        return active

    @property
    def native_value(self) -> str:
        active = self._active()
        if not active:
            return NONE_RUNNING
        coord = self.coordinator
        texts = [
            f"{coord.label_of(p) or UNLABELLED} · {price_text(p)}"
            for _, _, p in active
        ]
        return " + ".join(texts)[:255]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        active = self._active()
        if not active:
            return {"label": None, "buy": None, "sell": None, "start": None, "end": None}
        start, end, p = active[0]
        attrs = {
            "label": self.coordinator.label_of(p) or UNLABELLED,
            "buy": p.get("buyPrice"),
            "sell": p.get("sellPrice"),
            "start": start.isoformat(),
            "end": end.isoformat(),
        }
        if len(active) > 1:
            attrs["overlapping"] = len(active)
        return attrs


class NextPromoSensor(_MinuteTick, SensorEntity):
    """Start time of the next promo window (not counting one running now)."""

    _attr_translation_key = "next_promo"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "next_promo")

    def _next(self):
        _, _, nxt = windows(self.coordinator.data or [], self.coordinator.tz)
        return nxt

    @property
    def native_value(self) -> datetime | None:
        nxt = self._next()
        return nxt[0] if nxt else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        nxt = self._next()
        if not nxt:
            return {}
        _, end, p = nxt
        return {
            "label": self.coordinator.label_of(p) or UNLABELLED,
            "end": end.isoformat(),
            "buy": p.get("buyPrice"),
            "sell": p.get("sellPrice"),
        }

"""Base entity: one device per Sigen station."""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


class SigenPromoEntity(CoordinatorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, key: str) -> None:
        super().__init__(coordinator)
        station = coordinator.station
        self._attr_unique_id = f"{station}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(station))},
            name="Sigen promotional tariffs",
            manufacturer="Sigenergy",
            model=f"Station {station}",
            entry_type=DeviceEntryType.SERVICE,
        )

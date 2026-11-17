"""Trip preparation settings for Evolute vehicles."""
from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EvoluteDataUpdateCoordinator
from .entity import build_device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add a select when a vehicle reports support and usable options."""
    coordinator: EvoluteDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    remaining = {car["car_id"]: car for car in coordinator.cars}

    @callback
    def async_add_supported_cars() -> None:
        data = coordinator.data or {}
        supported = [
            car_id for car_id in remaining
            if data.get(car_id, {}).get("prep_duration_supported") is True
            and coordinator.trip_preparation_duration_options(car_id)
        ]
        if supported:
            async_add_entities([
                EvoluteTripPreparationDuration(coordinator, remaining.pop(car_id))
                for car_id in supported
            ])

    async_add_supported_cars()
    if remaining:
        entry.async_on_unload(coordinator.async_add_listener(async_add_supported_cars))


class EvoluteTripPreparationDuration(CoordinatorEntity, SelectEntity):
    """The server-side duration of preparation for one vehicle."""

    _attr_name = "Trip Preparation Duration"
    _attr_icon = "mdi:timer-cog-outline"

    def __init__(
        self, coordinator: EvoluteDataUpdateCoordinator, car: dict[str, Any]
    ) -> None:
        super().__init__(coordinator)
        self._car_id = car["car_id"]
        self._attr_unique_id = f"evolute_{car['local_id']}_prep_script_time"
        self.entity_id = f"select.{self._attr_unique_id}"
        self._attr_device_info = build_device_info(car)

    @property
    def available(self) -> bool:
        return self.coordinator.can_set_trip_preparation_duration(self._car_id)

    @property
    def options(self) -> list[str]:
        return [
            f"{minutes} min"
            for minutes in self.coordinator.trip_preparation_duration_options(self._car_id)
        ]

    @property
    def current_option(self) -> str | None:
        car = (self.coordinator.data or {}).get(self._car_id, {})
        minutes = car.get("prep_script_time")
        options = self.coordinator.trip_preparation_duration_options(self._car_id)
        if type(minutes) is int and minutes in options:
            return f"{minutes} min"
        return None

    async def async_select_option(self, option: str) -> None:
        for minutes in self.coordinator.trip_preparation_duration_options(self._car_id):
            if option == f"{minutes} min":
                await self.coordinator.async_set_trip_preparation_duration(self._car_id, minutes)
                return
        raise HomeAssistantError("Evolute trip preparation duration is outside the available options")

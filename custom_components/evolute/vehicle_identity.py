"""Persistent, installation-local identities for Evolute vehicles."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, TypedDict
from uuid import UUID, uuid4

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers.storage import Store

from .const import DOMAIN

STORAGE_KEY = f"{DOMAIN}.vehicle_identities"


class VehicleIdentity(TypedDict):
    uuid: str
    number: int


def _store_exists(path: Path) -> bool:
    """Distinguish a first run from a store renamed by HA after corruption."""
    try:
        path.lstat()
    except FileNotFoundError:
        try:
            if any(item.name.startswith(f"{path.name}.corrupt.") for item in path.parent.iterdir()):
                raise HomeAssistantError("Evolute vehicle identity storage needs recovery")
        except FileNotFoundError:
            pass
        return False
    return True


def _validate_identities(data: object) -> dict[str, VehicleIdentity]:
    if not isinstance(data, dict) or not data:
        raise ValueError("Invalid identity storage")
    uuids: set[str] = set()
    numbers: set[int] = set()
    for key, identity in data.items():
        car_id = json.loads(key)
        if (
            not car_id
            or not isinstance(car_id, (str, int, float))
            or json.dumps(car_id, ensure_ascii=False) != key
        ):
            raise ValueError("Invalid vehicle storage key")
        if not isinstance(identity, dict):
            raise ValueError("Invalid vehicle identity")
        value = identity.get("uuid")
        number = identity.get("number")
        if not isinstance(value, str):
            raise ValueError("Invalid vehicle UUID")
        parsed = UUID(value)
        if parsed.hex != value or parsed.version != 4 or value in uuids:
            raise ValueError("Invalid or duplicate vehicle UUID")
        if type(number) is not int or number < 1 or number in numbers:
            raise ValueError("Invalid or duplicate vehicle number")
        uuids.add(value)
        numbers.add(number)
    return data


class VehicleIdentityStore:
    """Allocate identities under one lock for all Evolute entries in this HA."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._lock = asyncio.Lock()

    def _new_store(self) -> Store[dict[str, VehicleIdentity]]:
        return Store(self._hass, 1, STORAGE_KEY, private=True, atomic_writes=True)

    async def async_assign(self, cars: list[dict[str, Any]]) -> None:
        """Attach local identities after confirming they are stored on disk."""
        # A setup cancelled while saving must not release the lock before the write ends.
        task = self._hass.async_create_task(self._async_get_identities(cars))
        try:
            identities = await asyncio.shield(task)
        except asyncio.CancelledError:
            task.add_done_callback(lambda result: None if result.cancelled() else result.exception())
            raise

        for car, identity in zip(cars, identities, strict=True):
            car["local_id"] = identity["uuid"]
            car["name"] = f"{car['model']} {identity['number']}"

    async def _async_get_identities(self, cars: list[dict[str, Any]]) -> list[VehicleIdentity]:
        keys = [json.dumps(car["car_id"], ensure_ascii=False) for car in cars]
        async with self._lock:
            store = self._new_store()
            try:
                existed = await self._hass.async_add_executor_job(_store_exists, Path(store.path))
                data = await store.async_load()
                if data is None and existed:
                    raise ValueError("Existing store could not be loaded")
                identities = {} if data is None else _validate_identities(data)
            except (HomeAssistantError, OSError, ValueError, TypeError, KeyError, NotImplementedError):
                raise ConfigEntryNotReady("Could not read Evolute vehicle identities") from None

            missing = sorted(set(keys) - identities.keys())
            if missing:
                if self._hass.is_stopping:
                    raise ConfigEntryNotReady("Cannot save Evolute vehicle identities while Home Assistant stops")
                last_number = max((identity["number"] for identity in identities.values()), default=0)
                updated = dict(identities)
                used = {identity["uuid"] for identity in identities.values()}
                for number, key in enumerate(missing, start=last_number + 1):
                    value = uuid4().hex
                    while value in used:
                        value = uuid4().hex
                    used.add(value)
                    updated[key] = {"uuid": value, "number": number}
                try:
                    await store.async_save(updated)
                    # Store may log and suppress write errors or defer writes at shutdown.
                    # A separate Store reads the saved data without the writer's pending state.
                    if await self._new_store().async_load() != updated:
                        raise ValueError("Identity storage write was not confirmed")
                except (HomeAssistantError, OSError, ValueError, TypeError, KeyError, NotImplementedError):
                    raise ConfigEntryNotReady("Could not save Evolute vehicle identities") from None
                identities = updated

            return [identities[key] for key in keys]


async def async_assign_vehicle_identities(hass: HomeAssistant, cars: list[dict[str, Any]]) -> None:
    if not cars:
        return
    store = hass.data.get(STORAGE_KEY)
    if store is None:
        store = hass.data[STORAGE_KEY] = VehicleIdentityStore(hass)
    await store.async_assign(cars)

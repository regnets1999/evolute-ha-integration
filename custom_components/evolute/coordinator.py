"""DataUpdateCoordinator for the Evolute integration."""
from __future__ import annotations

import logging
from asyncio import Lock
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import EvoluteAuthError, EvoluteClient, EvoluteError, TripPreparationRun
from .const import (
    COMMAND_CANCEL,
    COMMAND_PENDING_TIMEOUT_SECONDS,
    COMMAND_PREPARE,
    COMMAND_REFRESH_DELAYS,
    COMMAND_STATUS_ERROR,
    COMMAND_TERMINAL_STATUSES,
    DETAILS_RETRY_INTERVAL_SECONDS,
    DETAILS_UPDATE_INTERVAL_SECONDS,
    DISABLED_COMMANDS_KEY,
    DOMAIN,
    UPDATE_INTERVAL_SECONDS,
)

_LOGGER = logging.getLogger(__name__)


def trip_preparation_options(value: Any) -> list[int]:
    """Return unique positive integer options in server order."""
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(
        minutes for minutes in value if type(minutes) is int and minutes > 0
    ))


@dataclass
class PendingCommand:
    """A command that was accepted by the API but not yet visible in telemetry."""

    confirm_key: str | None
    baseline: Any
    expires_at: float
    # Set on the carV2 path, where the backend tracks the command itself and
    # reports whether the car accepted or rejected it.
    command_id: str | None = None


@dataclass
class PreparationTiming:
    """The last observed run and its fixed estimate, including terminal runs."""

    started_at: datetime
    ends_at: datetime | None = None
    confirmed: bool = False
    closed: bool = False


def _preparation_end(
    started_at: datetime, duration_ms: float | None, minutes: Any,
) -> datetime | None:
    configured_ms = minutes * 60_000 if type(minutes) is int and minutes > 0 else None
    for milliseconds in (configured_ms, duration_ms):
        if milliseconds is not None:
            try:
                return started_at + timedelta(milliseconds=milliseconds)
            except OverflowError:
                continue
    return None


class EvoluteDataUpdateCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Coordinator polling telemetry for every car on the account."""

    def __init__(
        self, hass: HomeAssistant, client: EvoluteClient, cars: list[dict[str, Any]]
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL_SECONDS),
        )
        self.client = client
        self.cars = cars
        self._details: dict[str, dict[str, Any]] = {}
        self._details_next_fetch = 0.0
        self._details_lock = Lock()
        self._preparation_locks: defaultdict[str, Lock] = defaultdict(Lock)
        self._preparation_timing: dict[str, PreparationTiming] = {}
        self._pending: dict[str, dict[str, PendingCommand]] = {}
        self._burst_cancels: list[Any] = []

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        """Poll Evolute and report confirmed account failures to Home Assistant."""
        try:
            return await self._async_poll()
        except EvoluteAuthError as err:
            for car_id in self._preparation_timing:
                self._invalidate_preparation_times(car_id)
            raise ConfigEntryAuthFailed(str(err)) from err

    async def _async_poll(self) -> dict[str, dict[str, Any]]:
        await self._async_update_details()

        telemetry: dict[str, dict[str, Any]] = {}
        last_error: EvoluteError | None = None

        for car in self.cars:
            car_id = car["car_id"]
            try:
                telemetry[car_id] = await self.client.async_get_car_info(car_id)
            except EvoluteAuthError:
                raise
            except EvoluteError as err:
                self._invalidate_preparation_times(car_id)
                last_error = err
                _LOGGER.debug("Could not update an Evolute vehicle: %s", err)
                continue

        if not telemetry and last_error is not None:
            raise UpdateFailed(str(last_error)) from last_error

        await self._async_resolve_command_ids()
        # Use the latest service data even if a write completed during this poll.
        data = {
            car_id: {**self._details.get(car_id, {}), **info}
            for car_id, info in telemetry.items()
        }
        now = datetime.now(UTC)
        for car_id, info in data.items():
            if "prep_run" in info:
                self._update_preparation_times(car_id, info, now)
        self._resolve_pending(data)
        return data

    def _invalidate_preparation_times(self, car_id: str) -> None:
        """Clear previously published times when telemetry can no longer confirm them."""
        timing = self._preparation_timing.get(car_id)
        if timing is None:
            return
        timing.confirmed = False
        current = (self.data or {}).get(car_id, {})
        if "prep_run" in current and (
            current.get("prep_start_time") is not None or current.get("prep_end_time") is not None
        ):
            self.data = {
                **self.data,
                car_id: {**current, "prep_start_time": None, "prep_end_time": None},
            }
            self.async_update_listeners()

    def _update_preparation_times(
        self, car_id: str, info: dict[str, Any], now: datetime,
    ) -> None:
        run: TripPreparationRun | None = info["prep_run"]
        timing = self._preparation_timing.get(car_id)
        info["prep_start_time"] = info["prep_end_time"] = None
        if timing is not None and timing.ends_at is not None and timing.ends_at <= now:
            timing.closed = True

        if not info.get("prep_run_cached"):
            if timing is not None:
                timing.confirmed = False
            started_at = run.started_at if run is not None else None
            if started_at is not None:
                if started_at > now or (timing is not None and started_at < timing.started_at):
                    return
                if timing is None or started_at > timing.started_at:
                    timing = self._preparation_timing[car_id] = PreparationTiming(started_at)
            if timing is None or run is None:
                return
            if run.is_active is False or run.finished_at is not None:
                timing.closed = True
            elif run.is_active is True and started_at is not None and not timing.closed:
                timing.confirmed = True
                if timing.ends_at is None:
                    timing.ends_at = _preparation_end(
                        started_at, run.duration_ms, info.get("prep_script_time")
                    )

        if timing is None or timing.closed or not timing.confirmed:
            return
        if timing.ends_at is not None and timing.ends_at <= now:
            timing.closed = True
            return
        info["prep_start_time"] = timing.started_at
        if timing.ends_at is not None:
            info["prep_end_time"] = timing.ends_at.isoformat()

    async def _async_update_details(self) -> None:
        """Refresh the slow-moving /car/v2/search fields when they are due."""
        if monotonic() < self._details_next_fetch:
            return
        async with self._details_lock:
            now = monotonic()
            if now < self._details_next_fetch:
                return

            try:
                details = await self.client.async_get_car_details()
            except EvoluteAuthError:
                raise
            except EvoluteError as err:
                _LOGGER.debug("Could not update Evolute service data: %s", err)
                # Back off briefly rather than retrying on every 30s telemetry poll.
                self._details_next_fetch = now + DETAILS_RETRY_INTERVAL_SECONDS
                return

            self._details = details
            self._details_next_fetch = now + DETAILS_UPDATE_INTERVAL_SECONDS

    def can_set_trip_preparation_duration(self, car_id: str) -> bool:
        """Return whether the current telemetry and details allow a write."""
        car = (self.data or {}).get(car_id, {})
        return (
            self.last_update_success
            and car.get("prep_duration_supported") is True
            and car.get("prep_running") is not True
            and bool(self.trip_preparation_duration_options(car_id))
        )

    def trip_preparation_duration_options(self, car_id: str) -> list[int]:
        """Return the latest server options, including a poll awaiting publication."""
        return trip_preparation_options(
            self._details.get(car_id, {}).get("prep_script_time_options")
        )

    async def async_set_trip_preparation_duration(self, car_id: str, minutes: int) -> None:
        """Write the setting, publish it, then reconcile it with the server."""
        async with self._preparation_locks[car_id], self._details_lock:
            if not self.can_set_trip_preparation_duration(car_id):
                raise HomeAssistantError("Evolute trip preparation duration cannot be changed now")
            options = self.trip_preparation_duration_options(car_id)
            if type(minutes) is not int or minutes not in options:
                raise HomeAssistantError(
                    "Evolute trip preparation duration is outside the available options"
                )

            try:
                await self.client.async_set_trip_preparation_duration(car_id, minutes)
            except EvoluteError as err:
                raise HomeAssistantError(str(err)) from err
            self._publish_trip_preparation_settings(car_id, {"prep_script_time": minutes})

            try:
                confirmed = await self.client.async_get_trip_preparation_duration(car_id)
            except EvoluteError as err:
                _LOGGER.debug("Could not verify the saved Evolute trip preparation duration: %s", err)
                return
            self._publish_trip_preparation_settings(car_id, confirmed)

    @callback
    def _publish_trip_preparation_settings(self, car_id: str, settings: dict[str, Any]) -> None:
        self._details.setdefault(car_id, {}).update(settings)
        if self.data and car_id in self.data:
            self.data = {**self.data, car_id: {**self.data[car_id], **settings}}
            self.async_update_listeners()

    # --- command dispatch -------------------------------------------------

    async def async_send_command(
        self, car_id: str, command: str, confirm_key: str | None
    ) -> bool:
        """Send a command and track it until the car confirms it in telemetry."""
        if command == COMMAND_PREPARE:
            async with self._preparation_locks[car_id]:
                if not self.last_update_success or not (self.data or {}).get(car_id):
                    raise HomeAssistantError("Evolute vehicle state is unavailable.")
                if self.is_command_blocked(car_id, command):
                    raise HomeAssistantError(
                        f"Evolute command '{command}' is currently blocked by the vehicle."
                    )
                return await self._async_send_command(car_id, command, confirm_key)
        return await self._async_send_command(car_id, command, confirm_key)

    async def _async_send_command(
        self, car_id: str, command: str, confirm_key: str | None
    ) -> bool:
        current = (self.data or {}).get(car_id, {})
        baseline = current.get(confirm_key) if confirm_key else None

        try:
            if command == COMMAND_PREPARE:
                duration = current.get("prep_script_time")
                options = current.get("prep_script_time_options")
                if (
                    type(duration) is not int or duration <= 0
                    or (isinstance(options, list) and duration not in trip_preparation_options(options))
                ):
                    duration = None
                accepted, command_id = await self.client.async_send_command(
                    car_id, command, duration_minutes=duration
                )
            else:
                accepted, command_id = await self.client.async_send_command(car_id, command)
        except EvoluteError as err:
            _LOGGER.warning("Could not send Evolute command: %s", err)
            return False
        if not accepted:
            return False

        # Only a command with an observable telemetry key is worth waiting for.
        # Blink changes nothing we can read, so it is done the moment the API
        # accepts it - marking it pending would just spin an indicator for
        # nothing and re-poll telemetry that cannot have changed.
        if confirm_key is None:
            return True

        self._pending.setdefault(car_id, {})[command] = PendingCommand(
            confirm_key=confirm_key,
            baseline=baseline,
            expires_at=monotonic() + COMMAND_PENDING_TIMEOUT_SECONDS,
            command_id=command_id,
        )
        self.async_update_listeners()
        self._schedule_command_refreshes()
        return True

    @callback
    def _schedule_command_refreshes(self) -> None:
        """Re-poll a few times after a command instead of waiting for the 30s tick."""
        self._cancel_command_refreshes()
        for delay in COMMAND_REFRESH_DELAYS:
            self._burst_cancels.append(
                async_call_later(self.hass, delay, self._async_burst_refresh)
            )

    @callback
    def _cancel_command_refreshes(self) -> None:
        for cancel in self._burst_cancels:
            cancel()
        self._burst_cancels.clear()

    async def _async_burst_refresh(self, _now: Any) -> None:
        await self.async_request_refresh()

    async def _async_resolve_command_ids(self) -> None:
        """Clear pending commands the backend has already settled.

        On the carV2 path a command carries an id whose status the backend
        resolves in a few seconds, so a rejected command can be reported right
        away instead of spinning until the pending timeout.
        """
        for car_id, commands in list(self._pending.items()):
            for command, pending in list(commands.items()):
                if pending.command_id is None:
                    continue
                try:
                    status = await self.client.async_get_command_status(car_id, pending.command_id)
                except EvoluteAuthError:
                    raise
                except EvoluteError as err:
                    _LOGGER.debug("Could not update Evolute command status: %s", err)
                    continue
                if status not in COMMAND_TERMINAL_STATUSES:
                    continue
                if status == COMMAND_STATUS_ERROR:
                    _LOGGER.warning(
                        "Evolute command %s was rejected by the vehicle",
                        command,
                    )
                del commands[command]
            if not commands:
                del self._pending[car_id]

    def _resolve_pending(self, data: dict[str, dict[str, Any]]) -> None:
        """Drop pending commands that the car has confirmed or that timed out."""
        now = monotonic()
        for car_id, commands in list(self._pending.items()):
            car_data = data.get(car_id, {})
            for command, pending in list(commands.items()):
                key = pending.confirm_key
                confirmed = key is not None and key in car_data and car_data[key] != pending.baseline
                if key == "prep_running" and command in (COMMAND_PREPARE, COMMAND_CANCEL):
                    confirmed = car_data.get(key) is (command == COMMAND_PREPARE)
                if confirmed or now >= pending.expires_at:
                    del commands[command]
            if not commands:
                del self._pending[car_id]

        if not self._pending:
            self._cancel_command_refreshes()

    def is_command_pending(self, car_id: str, command: str) -> bool:
        """Return True while a sent command has not yet shown up in telemetry."""
        return command in self._pending.get(car_id, {})

    def command_block_reason(self, car_id: str) -> str | None:
        """Return why the vehicle is refusing commands right now, if it is."""
        return (self.data or {}).get(car_id, {}).get("command_block_reason")

    def is_command_blocked(self, car_id: str, command: str) -> bool:
        """Return True if the backend currently reports the command as disabled."""
        disabled = (self.data or {}).get(car_id, {}).get(DISABLED_COMMANDS_KEY)
        return bool(disabled) and command in disabled

    async def async_shutdown(self) -> None:
        """Cancel any scheduled post-command refreshes on unload."""
        self._cancel_command_refreshes()
        await super().async_shutdown()

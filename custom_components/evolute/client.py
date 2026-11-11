"""Evolute (app.evassist.ru) API client for Home Assistant."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Optional

import aiohttp

from homeassistant.util import dt as dt_util

from .const import (
    BASE_URL,
    CAR_SEARCH_URL,
    COMMAND_CANCEL,
    COMMAND_PREPARE,
    COOKIE_ACCESS,
    COOKIE_REFRESH,
    BLOCK_NOT_PARKED,
    BLOCK_OFFLINE,
    BLOCK_UNLOCKED,
    CENTRAL_LOCK_TAG,
    DISABLED_COMMANDS_KEY,
    REFRESH_URL,
    TRIP_PREPARATION_TAGS,
    USER_FLAGS_URL,
    TELEMETRY_COMMANDS_URL,
    V2_COMMAND_BUTTONS,
)

_LOGGER = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)


class EvoluteError(Exception):
    """An API failure with a message safe to expose in Home Assistant."""


class EvoluteAuthError(EvoluteError):
    """The account credentials could not be restored."""


class EvoluteUnavailableError(EvoluteError):
    """A temporary HTTP or transport failure."""


class EvoluteProtocolError(EvoluteError):
    """The server response did not have the expected format."""


class EvoluteRequestError(EvoluteError):
    """The endpoint rejected the operation without confirming an account failure."""


@dataclass
class _RefreshAttempt:
    failure: tuple[type[EvoluteError], str] | None = None


def _raise_for_status(status: int, operation: str) -> None:
    if 200 <= status < 300:
        return
    message = f"Evolute {operation} failed (HTTP {status})"
    if status >= 500 or status in (408, 429):
        raise EvoluteUnavailableError(message)
    raise EvoluteRequestError(message)


async def _async_read_object(
    response: aiohttp.ClientResponse, *, allow_empty: bool = False
) -> dict[str, Any]:
    body = await response.read()
    if allow_empty and not body.strip():
        return {}
    try:
        data = json.loads(body)
    except ValueError:
        raise EvoluteProtocolError("Evolute returned invalid JSON") from None
    if not isinstance(data, dict):
        raise EvoluteProtocolError("Evolute returned an unexpected JSON structure")
    return data


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    """Decode a JWT payload without verifying the signature."""
    try:
        header, payload_b64, signature = token.split(".")
        if not (header and payload_b64 and signature):
            return {}
        padding = "=" * (-len(payload_b64) % 4)
        payload = base64.urlsafe_b64decode(payload_b64 + padding)
        data = json.loads(payload)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def get_account_id(access_token: str | None) -> str | None:
    """Read a stable account ID from a current or previously accepted access JWT."""
    if not isinstance(access_token, str):
        return None
    payload = _decode_jwt_payload(access_token)
    for claim in ("_id", "userId"):
        value = payload.get(claim)
        if isinstance(value, str) and value and value == value.strip():
            return value
    return None


def _local_utc_offset_hours() -> int:
    """Return the current UTC offset of the host, in whole hours."""
    offset = datetime.now().astimezone().utcoffset()
    if offset is None:
        return 0
    return int(offset.total_seconds() // 3600)


def _parse_iso_timestamp(raw: Any) -> Any:
    """Parse an ISO-8601 string from the API into an aware datetime, or None."""
    if raw in (None, ""):
        return None
    return dt_util.parse_datetime(str(raw))


def _parse_epoch_ms(raw: Any) -> Any:
    """Parse a millisecond epoch (the API sends it as a string) into a datetime."""
    if raw in (None, ""):
        return None
    try:
        return dt_util.utc_from_timestamp(int(raw) / 1000)
    except (ValueError, TypeError):
        return None


def _parse_car_info(raw: dict[str, Any]) -> dict[str, Any]:
    """Flatten the /car-service/tbox/{car_id}/info response for entity consumption."""
    sensors = raw.get("sensors") or {}
    sensors_data = sensors.get("sensorsData") or {}
    position = sensors.get("positionData") or {}
    prep = raw.get("preparation_script") or {}
    chip = sensors.get("chip") or {}
    warnings = raw.get("warnings") or []

    last_online = None
    last_online_raw = raw.get("lastOnlineTime")
    if last_online_raw not in (None, ""):
        try:
            last_online = dt_util.utc_from_timestamp(int(last_online_raw))
        except (ValueError, TypeError):
            last_online = None

    telemetry_time = None
    telemetry_time_raw = sensors.get("time")
    if telemetry_time_raw not in (None, ""):
        try:
            telemetry_time = dt_util.utc_from_timestamp(int(telemetry_time_raw))
        except (ValueError, TypeError):
            telemetry_time = None

    is_parked = bool(raw.get("isParked"))

    # The API reports centralLockingStatus as 1 = locked / 0 = unlocked, while HA's
    # LOCK binary sensor device class is inverted ("on" means unlocked), so flip it
    # here to keep the entity showing Locked when the car is actually locked.
    central_locking = sensors_data.get("centralLockingStatus")
    central_lock_open = None if central_locking is None else not central_locking

    # The remote-control buttons carry the only server-side feedback for heating and
    # cooling: sensorsData has no field for either, but buttons.main[] reports the
    # toggle state the app renders, plus whether the command is currently accepted.
    buttons = raw.get("buttons") or {}
    button_rows: list[dict[str, Any]] = []
    for group in ("main", "climate"):
        rows = buttons.get(group)
        if isinstance(rows, list):
            button_rows.extend(row for row in rows if isinstance(row, dict))
    by_command = {row["command"]: row for row in button_rows if row.get("command")}

    def _button_state(command: str) -> bool | None:
        row = by_command.get(command)
        if row is None:
            return None
        return row.get("state")

    disabled_commands = {
        command for command, row in by_command.items() if row.get("disabled")
    }

    # PREPARE / CANCEL are absent from buttons.main[], so their availability has to
    # be derived from preparation_script. They are the two halves of one toggle: the
    # app offers PREPARE while the script is idle and CANCEL while it runs.
    prep_running = bool(prep.get("running"))
    if prep_running or prep.get("disabled") or not prep.get("available"):
        disabled_commands.add(COMMAND_PREPARE)
    if not prep_running:
        disabled_commands.add(COMMAND_CANCEL)

    return {
        "battery_voltage": sensors_data.get("12VBatteryVoltage"),
        "odometer": sensors_data.get("odometer"),
        "battery_percentage": sensors_data.get("batteryPercentage"),
        "remains_mileage": sensors_data.get("remainsMileage"),
        "ignition": sensors_data.get("ignitionStatus"),
        "fuel_percentage": sensors_data.get("fuelPercentage"),
        "remains_mileage_fuel": sensors_data.get("remainsMileageFuel"),
        "central_lock": central_lock_open,
        "door_fl": sensors_data.get("doorFLStatus"),
        "door_fr": sensors_data.get("doorFRStatus"),
        "door_rl": sensors_data.get("doorRLStatus"),
        "door_rr": sensors_data.get("doorRRStatus"),
        "trunk": sensors_data.get("trunkStatus"),
        "headlights": sensors_data.get("headLightsStatus"),
        "battery_temp": sensors_data.get("batteryTemp"),
        "coolant_temp": sensors_data.get("coolantTemp"),
        "climate_current_temp": sensors_data.get("climateCurentTemp"),
        "climate_target_temp": sensors_data.get("climateTargetTemp"),
        "climate_fan_speed": sensors_data.get("climateFanSpeed"),
        "inboard_temp": sensors_data.get("inBoardTemp"),
        "outside_temp": sensors_data.get("outsideTemp"),
        "climate_status": sensors_data.get("climateStatus"),
        "climate_front_window": sensors_data.get("climateFWindowStatus"),
        "latitude": position.get("lat"),
        "longitude": position.get("lon"),
        "speed": position.get("speed"),
        "course": position.get("course"),
        "altitude": position.get("height"),
        "satellites": position.get("sats"),
        "hdop": position.get("hdop"),
        "is_online": bool(raw.get("isOnline")),
        "is_parked": is_parked,
        "is_moving": not is_parked,
        "last_online": last_online,
        "telemetry_time": telemetry_time,
        "status_text": chip.get("title"),
        "heating": _button_state("heating"),
        "cooling": _button_state("cooling"),
        "warnings_count": len(warnings),
        "has_warnings": bool(warnings),
        "prep_running": prep_running,
        "prep_available": bool(prep.get("available")),
        "prep_disabled": bool(prep.get("disabled")),
        "prep_error": bool(prep.get("errorStatus")),
        "prep_end_time": prep.get("endTime"),
        "prep_start_time": _parse_epoch_ms(prep.get("startTime")),
        # Not an entity state: consumed by the button platform to grey out a
        # command the backend is currently refusing.
        DISABLED_COMMANDS_KEY: disabled_commands,
    }


def _status_chip(sensors: dict[str, Any], is_online: Any) -> str:
    """Reproduce the status chip the web app renders from the same telemetry.

    carV2 has no chip object, and onlineState only ever says connected /
    disconnected - which the Online binary sensor already reports. The label the
    user sees in the app ("В пути", "Идет зарядка", ...) is derived client-side,
    so derive it the same way here.
    """
    if not is_online:
        return "Не в сети"
    voltage = sensors.get("12VBatteryVoltage")
    charging = (
        sensors.get("isChargingGunInserted") is True
        or sensors.get("chargingStatus") == 1
    )
    if charging and isinstance(voltage, (int, float)) and voltage >= 13:
        return "Идет зарядка"
    if sensors.get("ready") is True or sensors.get("isParkedOn") is False:
        return "В пути"
    if sensors.get("isIgnitionOn") is True or sensors.get("ignitionStatus") == 1:
        return "Зажигание включено"
    return "Доступен"


def _parse_car_info_v2(
    raw: dict[str, Any], tagged_buttons_visible: bool = True
) -> dict[str, Any]:
    """Normalize carV2 telemetry without mixing in stale legacy sensor values."""
    sensors = dict(raw.get("sensors") or {})
    for key, value in sensors.items():
        if isinstance(value, str):
            try:
                sensors[key] = float(value)
            except ValueError:
                pass
    aliases = {
        "centralLockingStatus": "isCentralLockingOn",
        "ignitionStatus": "isIgnitionOn",
        "trunkStatus": "isTrunkOpen",
        "remainsMileage": "remainsBatteryMileage",
        "remainsMileageFuel": "remainsFuelMileage",
    }
    for old, new in aliases.items():
        if new in sensors:
            sensors[old] = sensors[new]
    normalized = {
        "sensors": {"sensorsData": sensors, "positionData": sensors},
        "isOnline": raw.get("isOnline"),
        "isParked": sensors.get("isParkedOn"),
        "warnings": raw.get("warnings"),
    }
    parsed = _parse_car_info(normalized)
    parsed["telemetry_time"] = _parse_epoch_ms(raw.get("carStateUpdatedAt"))
    parsed["last_online"] = parsed["telemetry_time"] if raw.get("isOnline") else None
    parsed["status_text"] = _status_chip(sensors, raw.get("isOnline"))
    parsed["online_state"] = raw.get("onlineState")
    parsed["position_time"] = _parse_epoch_ms(raw.get("positionAt"))
    parsed["signal_level"] = sensors.get("signalLevel")
    parsed["charging_gun"] = sensors.get("isChargingGunInserted")
    parsed["is_charging"] = parsed["status_text"] == "Идет зарядка"
    parsed["car_state_ready"] = raw.get("isCarStateReady")
    firmware = raw.get("firmwareCheck") or {}
    settings = raw.get("settingsCheck") or {}
    parsed["firmware_version"] = firmware.get("actual")
    parsed["firmware_expected"] = firmware.get("expected")
    parsed["firmware_mismatch"] = bool(firmware.get("mismatch"))
    parsed["settings_mismatch"] = bool(settings.get("mismatch"))
    commands = {"heatingOn": "heating", "coolingOn": "cooling",
                "centralLockingOff": "centralLockingToggle", "search": "blink",
                "trunkOpen": "trunkToggle", "tripPreparationOn": COMMAND_PREPARE}
    disabled = set()
    tags: dict[str, str | None] = {}
    for button in raw.get("buttons") or []:
        command = commands.get(button.get("activateCommand"))
        if command in ("heating", "cooling"):
            parsed[command] = button.get("status")
        if command:
            tags[command] = button.get("tag")
        if command and not button.get("enabled", False):
            disabled.add(command)
        if button.get("activateCommand") == "tripPreparationOn":
            parsed["prep_running"] = bool(button.get("status"))
            parsed["prep_available"] = bool(button.get("enabled"))
            parsed["prep_disabled"] = not bool(button.get("enabled"))
    if parsed["prep_running"] or not parsed["prep_available"]:
        disabled.add(COMMAND_PREPARE)
    if not parsed["prep_running"]:
        disabled.add(COMMAND_CANCEL)

    # buttons[].enabled is only one of the reasons a control is refused. The app
    # greys the rest out from the same telemetry, so without them we offer
    # commands the car will not accept: nothing is dispatched unless it reports
    # itself parked and online, and while the central lock is open only the
    # central-lock group stays live. CANCEL shares PREPARE's button and tag.
    tags[COMMAND_CANCEL] = tags.get(COMMAND_PREPARE)
    if sensors.get("isParkedOn") is not True:
        block_reason = BLOCK_NOT_PARKED
    elif not raw.get("isOnline"):
        block_reason = BLOCK_OFFLINE
    elif sensors.get("isCentralLockingOn") is False:
        block_reason = BLOCK_UNLOCKED
    else:
        block_reason = None

    if block_reason == BLOCK_UNLOCKED:
        disabled.update(
            command
            for command in V2_COMMAND_BUTTONS
            if tags.get(command) != CENTRAL_LOCK_TAG
        )
    elif block_reason is not None:
        disabled.update(V2_COMMAND_BUTTONS)
    parsed["command_block_reason"] = block_reason

    # Trip preparation is a tagged button, which the app renders only for a
    # privileged account. Without the flag there is nothing to act on and
    # nothing to compare against, so report no state rather than a running
    # script the user cannot see, start or stop.
    hidden = {
        command for command, tag in tags.items() if tag in TRIP_PREPARATION_TAGS
    }
    if hidden and not tagged_buttons_visible:
        disabled.update(hidden)
        for key in ("prep_running", "prep_available", "prep_disabled", "prep_error"):
            parsed[key] = None
    if "isParkedOn" not in sensors:
        parsed["is_parked"] = parsed["is_moving"] = None
    parsed[DISABLED_COMMANDS_KEY] = disabled
    return parsed


def _parse_car_details(row: dict[str, Any]) -> dict[str, Any]:
    """Flatten the slow-moving fields of a /car/v2/search row (service, OTA, ...)."""
    maintenance = row.get("maintenance") or {}
    meta = maintenance.get("meta") or {}
    next_service = maintenance.get("next") or {}

    return {
        "maintenance_status": maintenance.get("status"),
        "maintenance_days_left": meta.get("daysLeft"),
        "maintenance_km_left": meta.get("kmLeft"),
        "maintenance_date": _parse_iso_timestamp(next_service.get("date")),
        "maintenance_mileage": next_service.get("mileage"),
        "update_available": bool(row.get("updateAvailable")),
        "location_enabled": bool(row.get("locationStatus")),
        "prep_script_time": row.get("currentScriptTime"),
        "last_sensor_request": _parse_iso_timestamp(row.get("lastSensorRequest")),
        "last_command_trigger": _parse_iso_timestamp(row.get("lastCommandTrigger")),
    }


class EvoluteClient:
    """Client for the Evolute (app.evassist.ru) car API.

    Authentication uses two cookies (evy-platform-access / evy-platform-refresh)
    obtained by the user from their browser session, since the native sign-in
    flow requires solving a Yandex SmartCaptcha before an SMS code can be
    requested.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        access_token: str,
        refresh_token: str,
        tokens_updated_callback: Optional[Callable[[str, str], None]] = None,
        *,
        expected_account_id: str | None = None,
    ) -> None:
        """Initialize the client."""
        self._session = session
        self.access_token = access_token
        self.refresh_token = refresh_token
        self._tokens_updated_callback = tokens_updated_callback
        self._expected_account_id = expected_account_id
        self._refresh_lock = asyncio.Lock()
        self._refresh_attempt = _RefreshAttempt()
        self._imeis: dict[str, str] = {}
        # Whether this account may see tag-gated controls. Assume it may until
        # the flags endpoint says otherwise, so a failed lookup never silently
        # removes a control that works.
        self._tagged_buttons_visible = True
        # Raw carV2 buttons[], kept per car so a command can be resolved to the
        # half of the toggle the app would send for the current state.
        self._buttons: dict[str, list[dict[str, Any]]] = {}
        self._etags: dict[str, str] = {}
        self._last_info: dict[str, dict[str, Any]] = {}
        # Cars whose carV2 telemetry profile does not exist (404). They are
        # served by the legacy tbox endpoint for the rest of this session.
        self._no_v2: set[str] = set()

    @property
    def user_id(self) -> str | None:
        """Return the account id embedded in the current access token, if any."""
        return get_account_id(self.access_token)

    def _cookies(self) -> dict[str, str]:
        return {
            COOKIE_ACCESS: self.access_token,
            COOKIE_REFRESH: self.refresh_token,
        }

    def _headers(self) -> dict[str, str]:
        return {
            "accept": "*/*",
            "content-type": "application/json",
            "user-agent": USER_AGENT,
            "x-app": "web",
            "time-zone": str(_local_utc_offset_hours()),
        }

    async def _async_refresh_if_needed(
        self, sent_cookies: dict[str, str], attempt: _RefreshAttempt
    ) -> None:
        """Refresh the credentials used by a request, or reuse its attempt's outcome."""
        async with self._refresh_lock:
            if sent_cookies != self._cookies():
                return
            if attempt is not self._refresh_attempt:
                if attempt.failure is not None:
                    error_type, message = attempt.failure
                    raise error_type(message)
                return

            attempt.failure = (
                EvoluteUnavailableError, "Evolute token refresh did not complete"
            )
            try:
                await self._async_refresh_tokens()
            except EvoluteError as err:
                attempt.failure = (type(err), str(err))
                raise
            else:
                attempt.failure = None
            finally:
                # Requests already sent share a failure; a later request can retry.
                self._refresh_attempt = _RefreshAttempt()

    async def _async_refresh_tokens(self) -> None:
        """Request and persist a new token pair while holding the refresh lock."""
        try:
            async with self._session.post(
                REFRESH_URL,
                json={"refreshToken": self.refresh_token},
                headers={"content-type": "application/json", "user-agent": USER_AGENT},
                timeout=REQUEST_TIMEOUT,
            ) as resp:
                if resp.status in (401, 403):
                    raise EvoluteAuthError(
                        f"Evolute rejected token refresh (HTTP {resp.status})"
                    )
                _raise_for_status(resp.status, "token refresh")
                data = await _async_read_object(resp)
        except (asyncio.TimeoutError, TimeoutError):
            raise EvoluteUnavailableError("Evolute token refresh timed out") from None
        except aiohttp.ClientError:
            raise EvoluteUnavailableError("Connection to Evolute failed during token refresh") from None

        access = data.get("accessToken")
        refresh = data.get("refreshToken")
        if not isinstance(access, str) or not access.strip():
            raise EvoluteProtocolError("Evolute refresh response lacks a valid access token")
        if refresh is not None and (
            not isinstance(refresh, str) or (refresh and not refresh.strip())
        ):
            raise EvoluteProtocolError("Evolute refresh response contains an invalid refresh token")

        if self._expected_account_id is not None:
            account_id = get_account_id(access)
            if account_id is None:
                raise EvoluteProtocolError("Refreshed Evolute access token lacks an account ID")
            if account_id != self._expected_account_id:
                raise EvoluteAuthError("Refreshed Evolute tokens belong to a different account")

        self.access_token = access
        if refresh:
            self.refresh_token = refresh
        if self._tokens_updated_callback:
            self._tokens_updated_callback(self.access_token, self.refresh_token)

    async def _async_search_cars(self) -> list[dict[str, Any]]:
        """Return the raw /car/v2/search rows for this account."""
        body = {
            "limit": 50,
            "offset": 0,
            # Both flags cost nothing extra on the wire and carry the service /
            # OTA / tracking fields that the tbox info endpoint does not report.
            "addSensors": True,
            "filters": [],
            "includeMaintenance": True,
        }
        result = await self._request("POST", CAR_SEARCH_URL, json_body=body)
        rows = result.get("rows")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise EvoluteProtocolError("Evolute returned an invalid vehicle list")
        if rows and not any(row.get("_id") for row in rows):
            raise EvoluteProtocolError("Evolute vehicle list lacks vehicle identifiers")
        return rows

    async def async_get_cars(self) -> list[dict[str, Any]]:
        """Return the list of cars for this account."""
        rows = await self._async_search_cars()

        try:
            await self._async_update_user_flags()
        except EvoluteAuthError:
            raise
        except EvoluteError as err:
            _LOGGER.debug("Could not update Evolute account flags: %s", err)

        cars = []
        for row in rows:
            model = row.get("carModel") or {}
            images = row.get("images") or {}
            car_id = row.get("_id")
            if not car_id:
                continue
            if (
                not isinstance(car_id, (str, int, float))
                or not isinstance(model, dict)
                or not isinstance(images, dict)
            ):
                raise EvoluteProtocolError("Evolute returned invalid vehicle metadata")
            try:
                details = await self._request("GET", f"{BASE_URL}/car-service/car/v2/{car_id}")
            except EvoluteAuthError:
                raise
            except EvoluteError as err:
                _LOGGER.debug("Could not obtain Evolute vehicle metadata: %s", err)
                details = {}
            imei = details.get("imei")
            if imei:
                self._imeis[car_id] = str(imei)
            cars.append(
                {
                    "car_id": car_id,
                    "vin": row.get("vin"),
                    "brand": row.get("brand") or "Evolute",
                    "model": model.get("name") or "Evolute",
                    "modification": model.get("modname"),
                    "model_year": model.get("modelYear"),
                    "color": model.get("color"),
                    "image": images.get("side"),
                    "name": row.get("vin") or model.get("name") or car_id,
                }
            )
        return cars

    async def async_get_car_details(self) -> dict[str, dict[str, Any]]:
        """Return slow-moving per-car data (service, OTA, tracking), keyed by car id."""
        rows = await self._async_search_cars()
        try:
            return {row["_id"]: _parse_car_details(row) for row in rows if row.get("_id")}
        except (AttributeError, TypeError, ValueError):
            raise EvoluteProtocolError("Evolute returned invalid service data") from None

    async def _async_update_user_flags(self) -> None:
        """Read the account permissions that decide which controls are offered."""
        flags = await self._request("GET", USER_FLAGS_URL)
        self._tagged_buttons_visible = bool(
            flags.get("isSuperAdmin") or flags.get("canViewTaggedCarButtons")
        )

    def _telemetry_imei(self, car_id: str) -> str | None:
        """The IMEI to read carV2 telemetry with, or None to use the old path.

        A car whose carModel has no modname has no carV2 profile, and the
        telemetry endpoint answers 404 for it however often it is asked, so
        once seen it is served by the legacy endpoint instead.
        """
        if car_id in self._no_v2:
            return None
        return self._imeis.get(car_id)

    async def async_get_car_info(self, car_id: str) -> dict[str, Any]:
        """Fetch and flatten telemetry for a single car."""
        imei = self._telemetry_imei(car_id)
        url = (f"{BASE_URL}/client-bff-service/telemetry/{imei}" if imei
               else f"{BASE_URL}/car-service/tbox/{car_id}/info")

        refreshed = False
        fell_back = False
        for _ in range(3):
            headers = self._headers()
            etag = self._etags.get(car_id)
            if etag:
                headers["if-none-match"] = etag

            sent_cookies = self._cookies()
            refresh_attempt = self._refresh_attempt
            try:
                async with self._session.get(
                    url,
                    headers=headers,
                    cookies=sent_cookies,
                    timeout=REQUEST_TIMEOUT,
                ) as resp:
                    if resp.status == 304:
                        if car_id not in self._last_info:
                            raise EvoluteProtocolError("Evolute returned 304 without cached telemetry")
                        return self._last_info[car_id]

                    if resp.status == 401 and not refreshed:
                        refreshed = True
                        resp.release()
                        await self._async_refresh_if_needed(sent_cookies, refresh_attempt)
                        continue

                    if resp.status == 404 and imei and not fell_back:
                        _LOGGER.warning(
                            "Evolute carV2 telemetry has no profile for %s "
                            "(HTTP 404); using the legacy tbox endpoint instead",
                            car_id,
                        )
                        self._no_v2.add(car_id)
                        self._buttons.pop(car_id, None)
                        # The etag belongs to the endpoint that just 404'd.
                        self._etags.pop(car_id, None)
                        fell_back = True
                        imei = None
                        url = f"{BASE_URL}/car-service/tbox/{car_id}/info"
                        continue

                    _raise_for_status(resp.status, "telemetry request")
                    raw = await _async_read_object(resp)
                    if not raw.keys() & {
                        "sensors", "isOnline", "onlineState", "isCarStateReady",
                        "isParked", "lastOnlineTime",
                    }:
                        raise EvoluteProtocolError("Evolute returned an invalid telemetry object")
                    if raw.get("sensors") is not None and not isinstance(raw["sensors"], dict):
                        raise EvoluteProtocolError("Evolute returned invalid telemetry sensors")
                    try:
                        if imei:
                            parsed = _parse_car_info_v2(raw, self._tagged_buttons_visible)
                            self._buttons[car_id] = [
                                button for button in (raw.get("buttons") or [])
                                if isinstance(button, dict)
                            ]
                        else:
                            parsed = _parse_car_info(raw)
                    except (AttributeError, TypeError, ValueError, OverflowError, OSError):
                        raise EvoluteProtocolError("Evolute returned invalid telemetry fields") from None

                    new_etag = resp.headers.get("etag")
                    if new_etag:
                        self._etags[car_id] = new_etag
                    self._last_info[car_id] = parsed
                    return parsed
            except (asyncio.TimeoutError, TimeoutError):
                raise EvoluteUnavailableError("Evolute telemetry request timed out") from None
            except aiohttp.ClientError:
                raise EvoluteUnavailableError("Connection to Evolute failed during telemetry request") from None

        raise EvoluteRequestError("Evolute telemetry retry limit reached")

    def _resolve_v2_command(self, car_id: str, command: str) -> str | None:
        """Map one of our command names onto the carV2 fnName to send.

        Every control is a two-sided toggle in buttons[]; the app sends the
        deactivate half when the button reads as on and the activate half
        otherwise. PREPARE / CANCEL are the two halves of one button, so they
        pick their side by name instead of by state.
        """
        target = V2_COMMAND_BUTTONS.get(command)
        if target is None:
            return None
        for button in self._buttons.get(car_id) or []:
            if button.get("activateCommand") != target:
                continue
            if command == COMMAND_PREPARE:
                return button.get("activateCommand")
            if command == COMMAND_CANCEL:
                return button.get("deactivateCommand")
            return (
                button.get("deactivateCommand")
                if button.get("status")
                else button.get("activateCommand")
            )
        return None

    async def async_send_command(
        self, car_id: str, command: str
    ) -> tuple[bool, str | None]:
        """Send a remote command. Returns (accepted, command_id).

        command_id is None on the legacy path, which reports nothing back and
        can only be confirmed by watching telemetry.
        """
        imei = self._telemetry_imei(car_id)
        if not imei:
            url = f"{BASE_URL}/car-service/tbox/{car_id}/{command}"
            await self._request("POST", url, json_body={}, allow_empty=True)
            return True, None

        fn_name = self._resolve_v2_command(car_id, command)
        if fn_name is None:
            _LOGGER.warning(
                "Evolute command %s has no matching carV2 button for %s",
                command,
                car_id,
            )
            return False, None

        result = await self._request(
            "POST",
            TELEMETRY_COMMANDS_URL,
            json_body={"commandName": fn_name, "imei": imei},
            allow_empty=True,
        )
        command_id = result.get("commandId")
        return True, str(command_id) if command_id else None

    async def async_get_command_status(
        self, car_id: str, command_id: str
    ) -> str | None:
        """Return pending / delivered / success / error for a sent command.

        The endpoint is keyed by IMEI, not by command id, and reports whatever
        command the car ran last - so the id it returns has to be matched
        against the one we sent, or a command issued from the app in the
        meantime would be read as the answer to ours. An unknown key is
        answered with an empty 200 rather than a 404, which lands here as a
        missing id and is treated the same way.
        """
        imei = self._telemetry_imei(car_id)
        if not imei:
            return None
        result = await self._request("GET", f"{TELEMETRY_COMMANDS_URL}/{imei}", allow_empty=True)
        if not result:
            return None
        if str(result.get("commandId") or "") != command_id:
            return None
        return result.get("status")

    async def _request(
        self,
        method: str,
        url: str,
        json_body: dict[str, Any] | None = None,
        *,
        allow_empty: bool = False,
    ) -> dict[str, Any]:
        account_request = url == CAR_SEARCH_URL
        for attempt in range(2):
            sent_cookies = self._cookies()
            refresh_attempt = self._refresh_attempt
            try:
                async with self._session.request(
                    method,
                    url,
                    json=json_body,
                    headers=self._headers(),
                    cookies=sent_cookies,
                    timeout=REQUEST_TIMEOUT,
                ) as resp:
                    refreshable = resp.status == 401 or (account_request and resp.status == 403)
                    if refreshable and attempt == 0:
                        resp.release()
                        await self._async_refresh_if_needed(sent_cookies, refresh_attempt)
                        continue
                    if account_request and resp.status == 401 and sent_cookies == self._cookies():
                        raise EvoluteAuthError("Evolute rejected refreshed account credentials (HTTP 401)")
                    _raise_for_status(resp.status, "API request")
                    return await _async_read_object(resp, allow_empty=allow_empty)
            except (asyncio.TimeoutError, TimeoutError):
                raise EvoluteUnavailableError("Evolute API request timed out") from None
            except aiohttp.ClientError:
                raise EvoluteUnavailableError("Connection to Evolute failed") from None

        raise EvoluteRequestError("Evolute API retry limit reached")

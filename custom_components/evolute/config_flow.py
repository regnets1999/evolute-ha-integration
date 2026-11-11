"""Config flow for the Evolute integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .client import (
    EvoluteAuthError,
    EvoluteClient,
    EvoluteError,
    EvoluteProtocolError,
    EvoluteUnavailableError,
    get_account_id,
)
from .const import CONF_ACCESS_TOKEN, CONF_REFRESH_TOKEN, DOMAIN

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ACCESS_TOKEN): str,
        vol.Required(CONF_REFRESH_TOKEN): str,
    }
)


class _AccountIdentityError(EvoluteError):
    """An account verification failure with a config flow translation key."""


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate connectivity and return account info and the current tokens."""
    session = async_get_clientsession(hass)
    client = EvoluteClient(session, data[CONF_ACCESS_TOKEN], data[CONF_REFRESH_TOKEN])

    cars = await client.async_get_cars()
    user_id = client.user_id
    if user_id is None:
        raise _AccountIdentityError("account_id_missing")
    title = f"Evolute ({cars[0]['name']})" if cars else "Evolute"
    return {
        "title": title,
        "user_id": user_id,
        "tokens": {
            CONF_ACCESS_TOKEN: client.access_token,
            CONF_REFRESH_TOKEN: client.refresh_token,
        },
    }


async def _validate_reauth(
    hass: HomeAssistant, entry: ConfigEntry, data: dict[str, Any]
) -> dict[str, Any]:
    # Older unique IDs can contain a refresh token fragment. The previously
    # saved access token provides the account identity independently of that ID.
    expected_id = get_account_id(entry.data.get(CONF_ACCESS_TOKEN))
    if expected_id is None:
        raise _AccountIdentityError("account_unverified")
    info = await validate_input(hass, data)
    if get_account_id(entry.data.get(CONF_ACCESS_TOKEN)) != expected_id:
        raise _AccountIdentityError("account_unverified")
    if info["user_id"] != expected_id:
        raise _AccountIdentityError("account_mismatch")
    return info


def _validation_error(error: EvoluteError) -> str:
    if isinstance(error, _AccountIdentityError):
        return str(error)
    if isinstance(error, EvoluteAuthError):
        return "invalid_auth"
    if isinstance(error, EvoluteUnavailableError):
        return "cannot_connect"
    if isinstance(error, EvoluteProtocolError):
        return "invalid_response"
    return "cannot_access"


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Evolute."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._reauth_entry: ConfigEntry | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                info = await validate_input(self.hass, user_input)
            except EvoluteError as err:
                errors["base"] = _validation_error(err)
            except Exception:  # pylint: disable=broad-except
                _LOGGER.error("Unexpected error validating Evolute credentials")
                errors["base"] = "unknown"
            else:
                account_id = info["user_id"]
                await self.async_set_unique_id(account_id)
                self._abort_if_unique_id_configured()
                # Legacy entries may have a different unique ID for this account.
                if any(
                    get_account_id(entry.data.get(CONF_ACCESS_TOKEN)) == account_id
                    for entry in self.hass.config_entries.async_entries(DOMAIN)
                ):
                    return self.async_abort(reason="already_configured")
                return self.async_create_entry(
                    title=info["title"], data={**user_input, **info["tokens"]}
                )

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> FlowResult:
        """Handle reauthentication triggered by ConfigEntryAuthFailed."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ask the user for fresh tokens."""
        errors: dict[str, str] = {}
        if user_input is not None:
            assert self._reauth_entry is not None
            try:
                info = await _validate_reauth(self.hass, self._reauth_entry, user_input)
            except EvoluteError as err:
                errors["base"] = _validation_error(err)
            except Exception:  # pylint: disable=broad-except
                _LOGGER.error("Unexpected error validating Evolute credentials")
                errors["base"] = "unknown"
            else:
                self.hass.config_entries.async_update_entry(
                    self._reauth_entry,
                    data={**self._reauth_entry.data, **info["tokens"]},
                )
                await self.hass.config_entries.async_reload(
                    self._reauth_entry.entry_id
                )
                return self.async_abort(reason="reauth_successful")

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

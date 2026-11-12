"""Offline tests for Evolute config flow, account identity, and token persistence."""

import json
import sys
from base64 import urlsafe_b64encode
from importlib import import_module
from pathlib import Path
from types import MappingProxyType, ModuleType, SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, Mock, patch


class ConfigFlowStub:
    def __init_subclass__(cls, *, domain):
        super().__init_subclass__()


class ConfigEntryAuthFailed(Exception):
    pass


class ConfigEntryNotReady(Exception):
    pass


class UpdateFailed(Exception):
    pass


class ClientError(Exception):
    pass


class DataUpdateCoordinatorStub:
    def __class_getitem__(cls, item):
        return cls

    def __init__(self, hass, *_args, **_kwargs):
        self.hass = hass


class CoordinatorEntityStub:
    def __init__(self, coordinator):
        self.coordinator = coordinator

    @property
    def unique_id(self):
        return self._attr_unique_id

    @property
    def device_info(self):
        return self._attr_device_info


class StoreStub:
    def __init__(self, hass, version, key, *, private, atomic_writes):
        assert version == 1 and private and atomic_writes
        self.hass = hass
        self.path = hass.storage_dir / key

    async def async_load(self):
        return await self.hass.load_identity_store(self.path)

    async def async_save(self, data):
        await self.hass.save_identity_store(self.path, data)


def load_evolute_module(name):
    """Load integration code with offline Home Assistant and aiohttp stubs."""
    package = ModuleType("_evolute_test")
    package.__path__ = [
        str(Path(__file__).resolve().parents[1] / "custom_components" / "evolute")
    ]
    config_entries = SimpleNamespace(ConfigFlow=ConfigFlowStub, ConfigEntry=object)
    modules = {
        package.__name__: package,
        "homeassistant": SimpleNamespace(config_entries=config_entries),
        "homeassistant.config_entries": config_entries,
        "homeassistant.const": SimpleNamespace(
            Platform=SimpleNamespace(
                SENSOR="sensor", BINARY_SENSOR="binary_sensor",
                BUTTON="button", DEVICE_TRACKER="device_tracker",
            )
        ),
        "homeassistant.core": SimpleNamespace(HomeAssistant=object, callback=lambda func: func),
        "homeassistant.data_entry_flow": SimpleNamespace(FlowResult=dict),
        "homeassistant.exceptions": SimpleNamespace(
            ConfigEntryAuthFailed=ConfigEntryAuthFailed,
            ConfigEntryNotReady=ConfigEntryNotReady,
            HomeAssistantError=Exception,
        ),
        "homeassistant.helpers.aiohttp_client": SimpleNamespace(
            async_get_clientsession=lambda hass: hass.session
        ),
        "homeassistant.helpers.event": SimpleNamespace(async_call_later=Mock()),
        "homeassistant.helpers.storage": SimpleNamespace(Store=StoreStub),
        "homeassistant.helpers.device_registry": SimpleNamespace(DeviceInfo=dict),
        "homeassistant.helpers.entity_platform": SimpleNamespace(AddEntitiesCallback=object),
        "homeassistant.helpers.update_coordinator": SimpleNamespace(
            DataUpdateCoordinator=DataUpdateCoordinatorStub, UpdateFailed=UpdateFailed,
            CoordinatorEntity=CoordinatorEntityStub,
        ),
        "homeassistant.util": SimpleNamespace(dt=object()),
        "homeassistant.components.binary_sensor": SimpleNamespace(
            BinarySensorDeviceClass=Mock(), BinarySensorEntity=object,
        ),
        "homeassistant.components.button": SimpleNamespace(ButtonEntity=object),
        "homeassistant.components.device_tracker": SimpleNamespace(
            SourceType=SimpleNamespace(GPS="gps"), TrackerEntity=object,
        ),
        "homeassistant.components.sensor": SimpleNamespace(
            SensorDeviceClass=Mock(), SensorStateClass=Mock(), SensorEntity=object,
        ),
        "aiohttp": SimpleNamespace(ClientTimeout=Mock(), ClientError=ClientError),
        "voluptuous": SimpleNamespace(
            Schema=lambda schema: schema, Required=lambda key: key
        ),
    }
    with patch.dict(sys.modules, modules):
        return import_module(f"{package.__name__}.{name}")


def load_config_flow():
    return load_evolute_module("config_flow")


def response(status, payload=None, *, body=None):
    context = MagicMock()
    context.__aenter__.return_value = SimpleNamespace(
        status=status,
        read=AsyncMock(return_value=json.dumps(payload).encode() if body is None else body),
        headers={},
        release=Mock(),
    )
    return context


def fake_access_jwt(payload):
    encoded = urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"e30.{encoded}.Zml4dHVyZQ"


class TokenPersistenceTests(IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.config_flow = load_config_flow()

    def setUp(self):
        self.account_id = "test-account"
        self.original = {
            "access_token": fake_access_jwt({"_id": self.account_id, "iat": 2}),
            "refresh_token": "test-refresh",
        }
        self.rotated = {
            "access_token": fake_access_jwt({"userId": self.account_id, "iat": 3}),
            "refresh_token": "rotated-test-refresh",
        }
        self.session = Mock()
        self.entry = SimpleNamespace(
            entry_id="test-entry",
            unique_id=self.account_id,
            data=MappingProxyType(
                {
                    "access_token": fake_access_jwt({"_id": self.account_id, "exp": 1}),
                    "refresh_token": "previous-test-refresh",
                    "extra_field": "keep-me",
                }
            ),
        )
        self.config_entries = SimpleNamespace(
            async_entries=Mock(return_value=[]),
            async_get_entry=Mock(return_value=self.entry),
            async_update_entry=Mock(),
            async_reload=AsyncMock(),
        )
        self.flow = self.config_flow.ConfigFlow()
        self.flow.hass = SimpleNamespace(
            session=self.session, config_entries=self.config_entries
        )
        self.flow.context = {"entry_id": self.entry.entry_id}
        self.flow.async_set_unique_id = AsyncMock()
        self.flow._abort_if_unique_id_configured = Mock()
        self.flow.async_create_entry = Mock()
        self.flow.async_show_form = Mock()
        self.flow.async_abort = Mock()

    def stub_api(self, *, rotate):
        replies = [response(200, {"rows": []}), response(200, {})]
        if rotate:
            replies.insert(0, response(401))
            self.session.post.return_value = response(
                200,
                {
                    "accessToken": self.rotated["access_token"],
                    "refreshToken": self.rotated["refresh_token"],
                },
            )
        self.session.request.side_effect = replies

    async def check_setup(self, *, rotate):
        self.stub_api(rotate=rotate)

        await self.flow.async_step_user(self.original.copy())

        expected = self.rotated if rotate else self.original
        self.flow.async_create_entry.assert_called_once_with(
            title="Evolute", data=expected
        )
        self.flow.async_set_unique_id.assert_awaited_once_with(self.account_id)
        self.flow._abort_if_unique_id_configured.assert_called_once()
        self.config_entries.async_entries.assert_called_once_with(self.config_flow.DOMAIN)
        self.assertEqual(self.session.post.call_count, int(rotate))

    async def check_setup_duplicate(self, *, rotate):
        previous_data = dict(self.entry.data)
        previous_unique_id = self.entry.unique_id
        self.config_entries.async_entries.return_value = [self.entry]
        self.stub_api(rotate=rotate)

        await self.flow.async_step_user(self.original.copy())

        self.config_entries.async_entries.assert_called_once_with(self.config_flow.DOMAIN)
        self.flow.async_set_unique_id.assert_awaited_once_with(self.account_id)
        self.flow._abort_if_unique_id_configured.assert_called_once()
        self.flow.async_abort.assert_called_once_with(reason="already_configured")
        self.flow.async_create_entry.assert_not_called()
        self.config_entries.async_update_entry.assert_not_called()
        self.config_entries.async_reload.assert_not_awaited()
        self.assertEqual(self.session.request.call_count, 2 + int(rotate))
        self.assertEqual(self.session.post.call_count, int(rotate))
        self.assertEqual(dict(self.entry.data), previous_data)
        self.assertEqual(self.entry.unique_id, previous_unique_id)

    async def check_reauth(self, *, rotate):
        self.stub_api(rotate=rotate)
        await self.flow.async_step_reauth(dict(self.entry.data))

        await self.flow.async_step_reauth_confirm(self.original.copy())

        expected = self.rotated if rotate else self.original
        self.config_entries.async_update_entry.assert_called_once_with(
            self.entry, data={**expected, "extra_field": "keep-me"}
        )
        self.config_entries.async_reload.assert_awaited_once_with(self.entry.entry_id)
        self.flow.async_abort.assert_called_once_with(reason="reauth_successful")
        self.assertEqual(self.session.post.call_count, int(rotate))
        self.flow.async_set_unique_id.assert_not_awaited()

    async def check_reauth_rejected(self, error):
        previous_data = dict(self.entry.data)
        await self.flow.async_step_reauth(previous_data)

        await self.flow.async_step_reauth_confirm(self.original.copy())

        self.assertEqual(self.flow.async_show_form.call_args.kwargs["errors"], {"base": error})
        self.config_entries.async_update_entry.assert_not_called()
        self.config_entries.async_reload.assert_not_awaited()
        self.flow.async_set_unique_id.assert_not_awaited()
        self.assertEqual(dict(self.entry.data), previous_data)

    async def test_setup_saves_rotated_tokens(self):
        self.original["access_token"] = fake_access_jwt({"_id": "expired-input-account"})
        await self.check_setup(rotate=True)

    async def test_reauth_saves_rotated_tokens_and_preserves_other_data(self):
        await self.check_reauth(rotate=True)

    async def test_setup_saves_original_tokens_when_refresh_is_unnecessary(self):
        await self.check_setup(rotate=False)

    async def test_setup_rejects_account_already_present_with_legacy_unique_id(self):
        self.entry.unique_id = "legacy-prefix-of-an-old-refresh-fixture"
        self.original["access_token"] = fake_access_jwt({"_id": "expired-input-account"})

        await self.check_setup_duplicate(rotate=True)

    async def test_setup_rejects_account_already_present_with_stable_unique_id(self):
        await self.check_setup_duplicate(rotate=False)

    async def test_setup_allows_a_different_account(self):
        self.entry.unique_id = "another-test-account"
        self.entry.data = MappingProxyType(
            {**self.entry.data, "access_token": fake_access_jwt({"_id": self.entry.unique_id})}
        )
        self.config_entries.async_entries.return_value = [self.entry]

        await self.check_setup(rotate=False)

    async def test_setup_ignores_entries_without_a_provable_account_id(self):
        self.entry.unique_id = None
        self.entry.data = MappingProxyType(
            {**self.entry.data, "access_token": fake_access_jwt({})}
        )
        self.config_entries.async_entries.return_value = [self.entry, SimpleNamespace(data={})]

        await self.check_setup(rotate=False)

    async def test_reauth_without_refresh_preserves_other_data(self):
        await self.check_reauth(rotate=False)

    async def test_setup_rejects_missing_or_invalid_account_id(self):
        for payload in ({}, {"_id": " "}, {"userId": ["test-account"]}):
            with self.subTest(payload=payload):
                self.original["access_token"] = fake_access_jwt(payload)
                self.stub_api(rotate=False)

                await self.flow.async_step_user(self.original.copy())

                self.assertEqual(
                    self.flow.async_show_form.call_args.kwargs["errors"],
                    {"base": "account_id_missing"},
                )
        self.flow.async_set_unique_id.assert_not_awaited()
        self.flow.async_create_entry.assert_not_called()

    async def test_reauth_rejects_different_account(self):
        self.original["access_token"] = fake_access_jwt({"_id": "another-test-account"})
        self.stub_api(rotate=False)

        await self.check_reauth_rejected("account_mismatch")

    async def test_reauth_compares_account_after_refresh(self):
        self.rotated["access_token"] = fake_access_jwt({"userId": "another-test-account"})
        self.stub_api(rotate=True)

        await self.check_reauth_rejected("account_mismatch")

        self.session.post.assert_called_once()

    async def test_reauth_rejects_candidate_without_account_id(self):
        self.original["access_token"] = fake_access_jwt({})
        self.stub_api(rotate=False)

        await self.check_reauth_rejected("account_id_missing")

    async def test_legacy_unique_id_is_preserved_after_refresh(self):
        self.entry.unique_id = "legacy-prefix-of-an-old-refresh-fixture"
        previous_id = self.entry.unique_id

        await self.check_reauth(rotate=True)

        self.assertEqual(self.entry.unique_id, previous_id)

    async def test_missing_unique_id_does_not_prevent_proven_reauth(self):
        self.entry.unique_id = None

        await self.check_reauth(rotate=False)

        self.assertIsNone(self.entry.unique_id)

    async def test_legacy_entry_without_saved_account_id_is_not_overwritten(self):
        self.entry.data = MappingProxyType(
            {**self.entry.data, "access_token": fake_access_jwt({})}
        )

        for unique_id in (None, "legacy-prefix-of-an-old-refresh-fixture", self.account_id):
            with self.subTest(unique_id=unique_id):
                self.entry.unique_id = unique_id
                await self.check_reauth_rejected("account_unverified")

        self.session.request.assert_not_called()
        self.session.post.assert_not_called()

    async def test_setup_network_failure_does_not_save_tokens(self):
        self.session.request.side_effect = ClientError("test connection failure")

        await self.flow.async_step_user(self.original.copy())

        self.assertEqual(self.flow.async_show_form.call_args.kwargs["errors"], {"base": "cannot_connect"})
        self.flow.async_create_entry.assert_not_called()
        self.session.post.assert_not_called()

    async def test_setup_refresh_rejection_displays_invalid_auth(self):
        self.session.request.return_value = response(401)
        self.session.post.return_value = response(403)

        await self.flow.async_step_user(self.original.copy())

        self.assertEqual(self.flow.async_show_form.call_args.kwargs["errors"], {"base": "invalid_auth"})
        self.flow.async_create_entry.assert_not_called()

    async def test_reauth_bad_json_does_not_save_tokens_or_reload(self):
        self.session.request.return_value = response(200, body=b"{")
        await self.flow.async_step_reauth(dict(self.entry.data))

        await self.flow.async_step_reauth_confirm(self.original.copy())

        self.assertEqual(self.flow.async_show_form.call_args.kwargs["errors"], {"base": "invalid_response"})
        self.config_entries.async_update_entry.assert_not_called()
        self.config_entries.async_reload.assert_not_awaited()

    async def test_reauth_refresh_503_displays_temporary_error(self):
        self.session.request.return_value = response(401)
        self.session.post.return_value = response(503)
        await self.flow.async_step_reauth(dict(self.entry.data))

        await self.flow.async_step_reauth_confirm(self.original.copy())

        self.assertEqual(self.flow.async_show_form.call_args.kwargs["errors"], {"base": "cannot_connect"})
        self.config_entries.async_update_entry.assert_not_called()
        self.config_entries.async_reload.assert_not_awaited()

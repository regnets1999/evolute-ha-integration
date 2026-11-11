"""Offline regression tests for saving the token pair validated by Evolute."""

import sys
from importlib import import_module
from pathlib import Path
from types import MappingProxyType, ModuleType, SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, Mock, patch


class ConfigFlowStub:
    def __init_subclass__(cls, *, domain):
        super().__init_subclass__()


def load_config_flow():
    """Load the real flow and API client without bootstrapping Home Assistant."""
    package = ModuleType("_evolute_test")
    package.__path__ = [
        str(Path(__file__).resolve().parents[1] / "custom_components" / "evolute")
    ]
    config_entries = SimpleNamespace(ConfigFlow=ConfigFlowStub, ConfigEntry=object)
    modules = {
        package.__name__: package,
        "homeassistant": SimpleNamespace(config_entries=config_entries),
        "homeassistant.config_entries": config_entries,
        "homeassistant.core": SimpleNamespace(HomeAssistant=object),
        "homeassistant.data_entry_flow": SimpleNamespace(FlowResult=dict),
        "homeassistant.exceptions": SimpleNamespace(HomeAssistantError=Exception),
        "homeassistant.helpers.aiohttp_client": SimpleNamespace(
            async_get_clientsession=lambda hass: hass.session
        ),
        "homeassistant.util": SimpleNamespace(dt=object()),
        "homeassistant.components.binary_sensor": SimpleNamespace(
            BinarySensorDeviceClass=Mock()
        ),
        "homeassistant.components.sensor": SimpleNamespace(
            SensorDeviceClass=Mock(), SensorStateClass=Mock()
        ),
        "aiohttp": SimpleNamespace(ClientTimeout=Mock()),
        "voluptuous": SimpleNamespace(
            Schema=lambda schema: schema, Required=lambda key: key
        ),
    }
    with patch.dict(sys.modules, modules):
        return import_module(f"{package.__name__}.config_flow")


def response(status, payload=None):
    context = MagicMock()
    context.__aenter__.return_value = SimpleNamespace(
        status=status, json=AsyncMock(return_value=payload), release=Mock()
    )
    return context


class TokenPersistenceTests(IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.config_flow = load_config_flow()

    def setUp(self):
        self.original = {"access_token": "test-access", "refresh_token": "test-refresh"}
        self.rotated = {
            "access_token": "rotated-test-access",
            "refresh_token": "rotated-test-refresh",
        }
        self.session = Mock()
        self.entry = SimpleNamespace(
            entry_id="test-entry",
            data=MappingProxyType(
                {
                    "access_token": "previous-test-access",
                    "refresh_token": "previous-test-refresh",
                    "extra_field": "keep-me",
                }
            ),
        )
        self.config_entries = SimpleNamespace(
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
        self.assertEqual(self.session.post.call_count, int(rotate))

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

    async def test_setup_saves_rotated_tokens(self):
        await self.check_setup(rotate=True)

    async def test_reauth_saves_rotated_tokens_and_preserves_other_data(self):
        await self.check_reauth(rotate=True)

    async def test_setup_saves_original_tokens_when_refresh_is_unnecessary(self):
        await self.check_setup(rotate=False)

    async def test_reauth_without_refresh_preserves_other_data(self):
        await self.check_reauth(rotate=False)

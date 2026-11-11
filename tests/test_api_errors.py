"""Offline coverage of Evolute API errors and Home Assistant lifecycle handling."""

from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

from test_config_flow import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    UpdateFailed,
    load_evolute_module,
    response,
)


class ApiErrorTests(IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.api = load_evolute_module("client")
        cls.integration = load_evolute_module("__init__")

    def setUp(self):
        self.session = Mock()
        self.persist = Mock()
        self.client = self.api.EvoluteClient(
            self.session, "test-access", "test-refresh", self.persist
        )
        self.hass = SimpleNamespace(
            session=self.session,
            data={},
            config_entries=SimpleNamespace(
                async_update_entry=Mock(), async_forward_entry_setups=AsyncMock()
            ),
        )
        self.entry = SimpleNamespace(
            entry_id="test-entry",
            data={"access_token": "test-access", "refresh_token": "test-refresh", "extra": True},
        )

    async def setup_entry(self):
        with (
            patch.object(self.integration, "_setup_www_files", new_callable=AsyncMock),
            patch.object(self.integration, "_register_card_resource", new_callable=AsyncMock),
            patch.object(
                self.integration.EvoluteDataUpdateCoordinator,
                "async_config_entry_first_refresh",
                new_callable=AsyncMock,
                create=True,
            ),
        ):
            return await self.integration.async_setup_entry(self.hass, self.entry)

    def coordinator(self, *car_ids):
        client = self.integration.EvoluteClient(self.session, "test-access", "test-refresh")
        return self.integration.EvoluteDataUpdateCoordinator(
            self.hass, client, [{"car_id": car_id} for car_id in car_ids]
        )

    async def test_startup_http_503_retries_without_refresh(self):
        self.session.request.return_value = response(503)

        with self.assertRaisesRegex(ConfigEntryNotReady, "HTTP 503"):
            await self.setup_entry()

        self.session.post.assert_not_called()
        self.hass.config_entries.async_update_entry.assert_not_called()
        self.hass.config_entries.async_forward_entry_setups.assert_not_awaited()

    async def test_startup_refresh_rejection_requires_reauth(self):
        for status in (401, 403):
            with self.subTest(status=status):
                self.session.request.return_value = response(401)
                self.session.post.return_value = response(status)

                with self.assertRaisesRegex(ConfigEntryAuthFailed, f"HTTP {status}"):
                    await self.setup_entry()

        self.assertEqual(self.session.post.call_count, 2)
        self.hass.config_entries.async_update_entry.assert_not_called()

    async def test_startup_persists_refresh_using_existing_entry(self):
        self.session.request.side_effect = [
            response(401), response(200, {"rows": []}), response(200, {}),
        ]
        self.session.post.return_value = response(
            200, {"accessToken": "rotated-test-access", "refreshToken": "rotated-test-refresh"}
        )

        self.assertTrue(await self.setup_entry())

        self.hass.config_entries.async_update_entry.assert_called_once_with(
            self.entry,
            data={
                "access_token": "rotated-test-access",
                "refresh_token": "rotated-test-refresh",
                "extra": True,
            },
        )

    async def test_refresh_timeout_is_temporary(self):
        self.session.request.return_value = response(401)
        self.session.post.side_effect = TimeoutError

        with self.assertRaisesRegex(self.api.EvoluteUnavailableError, "timed out"):
            await self.client.async_get_cars()

        self.session.request.assert_called_once()
        self.persist.assert_not_called()

    async def test_bad_refresh_response_is_protocol_error(self):
        self.session.request.return_value = response(401)
        for reply in (
            response(200, body=b"{"),
            response(200, {"accessToken": []}),
            response(200, {"accessToken": "rotated-test-access", "refreshToken": 42}),
        ):
            with self.subTest():
                self.session.post.return_value = reply
                with self.assertRaises(self.api.EvoluteProtocolError):
                    await self.client.async_get_cars()

        self.persist.assert_not_called()
        self.assertEqual(self.client.access_token, "test-access")

    async def test_search_rejects_bad_response_structure(self):
        for payload in (None, [], {}, {"rows": "invalid"}, {"rows": [None]}, {"rows": [{}]}):
            with self.subTest(payload=payload):
                self.session.request.return_value = response(200, payload)

                with self.assertRaises(self.api.EvoluteProtocolError):
                    await self.client.async_get_cars()

        self.session.post.assert_not_called()

    async def test_repeated_account_401_confirms_auth_failure(self):
        self.session.request.side_effect = [response(401), response(401)]
        self.session.post.return_value = response(200, {"accessToken": "rotated-test-access"})

        with self.assertRaisesRegex(self.api.EvoluteAuthError, "HTTP 401"):
            await self.client.async_get_cars()

        self.session.post.assert_called_once()
        self.persist.assert_called_once_with("rotated-test-access", "test-refresh")

    async def test_repeated_account_403_remains_an_endpoint_denial(self):
        self.session.request.side_effect = [response(403), response(403)]
        self.session.post.return_value = response(200, {"accessToken": "rotated-test-access"})

        with self.assertRaisesRegex(self.api.EvoluteRequestError, "HTTP 403"):
            await self.client.async_get_cars()

        self.session.post.assert_called_once()

    async def test_poll_recovers_after_temporary_failure(self):
        coordinator = self.coordinator("test-car")
        self.session.request.return_value = response(200, {"rows": []})
        self.session.get.side_effect = [response(503), response(200, {"isOnline": True})]

        with self.assertRaisesRegex(UpdateFailed, "HTTP 503"):
            await coordinator._async_update_data()

        data = await coordinator._async_update_data()

        self.assertTrue(data["test-car"]["is_online"])
        self.session.post.assert_not_called()

    async def test_poll_auth_failure_is_reported_to_home_assistant(self):
        coordinator = self.coordinator("test-car")
        self.session.request.return_value = response(200, {"rows": []})
        self.session.get.return_value = response(401)
        self.session.post.return_value = response(403)

        with self.assertRaisesRegex(ConfigEntryAuthFailed, "HTTP 403"):
            await coordinator._async_update_data()

        self.session.post.assert_called_once()

    async def test_partial_poll_preserves_other_cars_and_cached_details(self):
        coordinator = self.coordinator("denied", "bad-response", "healthy")
        coordinator._details = {"healthy": {"maintenance_days_left": 20}}
        self.session.request.return_value = response(503)
        self.session.get.side_effect = [
            response(403),
            response(200, {"sensors": "invalid"}),
            response(200, {"isOnline": True}),
        ]

        data = await coordinator._async_update_data()

        self.assertEqual(set(data), {"healthy"})
        self.assertTrue(data["healthy"]["is_online"])
        self.assertEqual(data["healthy"]["maintenance_days_left"], 20)
        self.session.post.assert_not_called()

    async def test_missing_car_v2_profile_still_uses_legacy_telemetry(self):
        self.client._imeis["test-car"] = "test-imei"
        self.client._etags["test-car"] = "test-etag"
        self.session.get.side_effect = [response(404), response(200, {"isOnline": True})]

        with self.assertLogs(self.api.__name__, level="WARNING"):
            data = await self.client.async_get_car_info("test-car")

        self.assertTrue(data["is_online"])
        car_v2, legacy = self.session.get.call_args_list
        self.assertTrue(car_v2.args[0].endswith("/telemetry/test-imei"))
        self.assertTrue(legacy.args[0].endswith("/tbox/test-car/info"))
        self.assertNotIn("if-none-match", legacy.kwargs["headers"])
        self.session.post.assert_not_called()

    async def test_command_allows_empty_success_but_rejects_malformed_json(self):
        self.session.request.side_effect = [response(204, body=b""), response(200, body=b"{")]

        self.assertEqual(await self.client.async_send_command("test-car", "blink"), (True, None))
        with self.assertRaises(self.api.EvoluteProtocolError):
            await self.client.async_send_command("test-car", "blink")

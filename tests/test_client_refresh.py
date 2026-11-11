"""Offline tests for concurrent token refresh in the Evolute API client."""

import asyncio
from unittest import IsolatedAsyncioTestCase
from unittest.mock import Mock

from test_config_flow import load_config_flow, response


class ConcurrentRefreshTests(IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.flow = load_config_flow()

    def setUp(self):
        self.session = Mock()
        self.persist = Mock()
        self.original = ("test-access", "test-refresh")
        self.rotated = ("rotated-test-access", "rotated-test-refresh")
        self.client = self.flow.EvoluteClient(
            self.session, *self.original, tokens_updated_callback=self.persist
        )
        self.telemetry = {"battery_percentage": 80}
        self.client._last_info["test-car"] = self.telemetry

    async def run_concurrent_requests(self, *, refresh_status, delayed_401=False):
        refresh_started = asyncio.Event()
        telemetry_sent = asyncio.Event()
        finish_refresh = asyncio.Event()
        finish_telemetry = asyncio.Event()
        request_done = asyncio.Event()

        telemetry_401 = response(401)
        telemetry_reply = telemetry_401.__aenter__.return_value

        async def get_old_telemetry():
            telemetry_sent.set()
            if delayed_401:
                await finish_telemetry.wait()
            return telemetry_reply

        refresh = response(
            refresh_status,
            {"accessToken": self.rotated[0], "refreshToken": self.rotated[1]},
        )
        refresh_reply = refresh.__aenter__.return_value

        async def refresh_tokens():
            refresh_started.set()
            await finish_refresh.wait()
            return refresh_reply

        telemetry_401.__aenter__.side_effect = get_old_telemetry
        refresh.__aenter__.side_effect = refresh_tokens
        self.session.request.side_effect = [response(401), response(200, {"rows": []})]
        self.session.get.side_effect = [telemetry_401, response(304)]
        self.session.post.side_effect = [refresh, response(403)]

        async def request_cars():
            try:
                return await self.client._request("POST", "https://example.invalid/cars")
            finally:
                request_done.set()

        async def release_responses():
            await refresh_started.wait()
            await telemetry_sent.wait()
            finish_refresh.set()
            if delayed_401:
                await request_done.wait()
                finish_telemetry.set()

        cars, telemetry, _ = await asyncio.wait_for(
            asyncio.gather(
                request_cars(),
                self.client.async_get_car_info("test-car"),
                release_responses(),
                return_exceptions=True,
            ),
            timeout=5,
        )
        return cars, telemetry

    def assert_rotated(self, cars, telemetry):
        self.assertEqual(cars, {"rows": []})
        self.assertEqual(telemetry, self.telemetry)
        self.assertEqual(self.session.post.call_count, 1)
        self.persist.assert_called_once_with(*self.rotated)
        for request in (self.session.request, self.session.get):
            self.assertEqual(request.call_count, 2)
            cookie_names = ("evy-platform-access", "evy-platform-refresh")
            for call, tokens in zip(request.call_args_list, (self.original, self.rotated)):
                self.assertEqual(call.kwargs["cookies"], dict(zip(cookie_names, tokens)))

    async def test_concurrent_401_responses_share_successful_refresh(self):
        self.assert_rotated(*await self.run_concurrent_requests(refresh_status=200))

    async def test_late_401_retries_with_rotated_tokens(self):
        self.assert_rotated(
            *await self.run_concurrent_requests(refresh_status=200, delayed_401=True)
        )

    async def test_concurrent_401_responses_share_rejected_refresh(self):
        result = await self.run_concurrent_requests(refresh_status=403)

        for error in result:
            self.assertIsInstance(error, self.flow.EvoluteAuthError)
            self.assertIn("HTTP 403", str(error))
        self.assertEqual(self.session.post.call_count, 1)
        self.session.request.assert_called_once()
        self.session.get.assert_called_once()
        self.persist.assert_not_called()
        self.assertEqual(
            (self.client.access_token, self.client.refresh_token), self.original
        )

    async def test_request_after_transient_failure_can_refresh_again(self):
        result = await self.run_concurrent_requests(refresh_status=503, delayed_401=True)

        for error in result:
            self.assertIsInstance(error, self.flow.EvoluteUnavailableError)
            self.assertIn("HTTP 503", str(error))
        self.assertEqual(self.session.post.call_count, 1)
        self.persist.assert_not_called()

        self.session.request.side_effect = [response(401), response(200, {"rows": []})]
        self.session.post.side_effect = [
            response(200, {"accessToken": self.rotated[0], "refreshToken": self.rotated[1]})
        ]
        self.assertEqual(
            await self.client._request("POST", "https://example.invalid/cars"),
            {"rows": []},
        )
        self.assertEqual(self.session.post.call_count, 2)
        self.persist.assert_called_once_with(*self.rotated)

"""Offline tests for trip preparation start and estimated end states."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

from test_config_flow import load_evolute_module, response


class TripPreparationTimesTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.api = load_evolute_module("client")
        self.module = load_evolute_module("coordinator")
        self.session = Mock()
        self.client = self.module.EvoluteClient(self.session, "test-access", "test-refresh")
        self.car = {"car_id": "car", "local_id": "local-car", "name": "Test car"}
        self.client._imeis["car"] = "test-imei"
        self.client._tagged_buttons_visible = True
        self.coordinator = self.module.EvoluteDataUpdateCoordinator(Mock(), self.client, [self.car])
        self.coordinator._details_next_fetch = float("inf")
        self.details = self.coordinator._details["car"] = {"prep_script_time": 30}
        self.started = datetime(2026, 11, 17, 16, 12, tzinfo=UTC)
        self.start_ms = int(self.started.timestamp() * 1000)
        self.now = self.started + timedelta(minutes=1)

    def payload(self, **overrides):
        run = {"isActive": True, "startedAt": self.start_ms, "durationMs": 900_000, **overrides}
        return {
            "isOnline": True,
            "sensors": {"isIgnitionOn": True},
            "buttons": [{"activateCommand": "tripPreparationOn", "status": True, "lastRun": run}],
        }

    async def poll(self, reply):
        self.session.get.return_value = reply
        with patch.object(self.module, "datetime") as clock:
            clock.now.return_value = self.now
            self.coordinator.data = await self.coordinator._async_poll()
        return self.coordinator.data["car"]

    def assert_times_unknown(self, info):
        self.assertIsNone(info["prep_start_time"])
        self.assertIsNone(info["prep_end_time"])

    async def test_parses_valid_car_v2_start_and_prefers_configured_duration(self):
        info = await self.poll(response(200, self.payload(finishedAt=0)))
        self.assertIs(info["prep_running"], True)
        self.assertEqual(info["prep_start_time"], self.started)
        self.assertEqual(info["prep_end_time"], "2026-11-17T16:42:00+00:00")

    async def test_falls_back_to_finite_run_duration(self):
        for minutes in (None, 0, -1, True, 30.0, "30", 10**400):
            with self.subTest(minutes=minutes):
                self.coordinator._preparation_timing.clear()
                self.details["prep_script_time"] = minutes
                info = await self.poll(response(200, self.payload(durationMs=720_500.5)))
                self.assertEqual(info["prep_start_time"], self.started)
                self.assertEqual(info["prep_end_time"], "2026-11-17T16:24:00.500500+00:00")

    async def test_missing_or_invalid_fields_do_not_invent_times(self):
        for value in (None, False, "", str(self.start_ms), -1, 10**400):
            with self.subTest(started_at=value):
                info = await self.poll(response(200, self.payload(startedAt=value)))
                self.assertIs(info["prep_running"], True)
                self.assert_times_unknown(info)
        for value in (None, [], {"isActive": "true", "startedAt": self.start_ms}):
            with self.subTest(last_run=value):
                payload = self.payload()
                if value is None:
                    del payload["buttons"][0]["lastRun"]
                else:
                    payload["buttons"][0]["lastRun"] = value
                info = await self.poll(response(200, payload))
                self.assertIsNone(info["prep_running"])
                self.assert_times_unknown(info)
        self.details["prep_script_time"] = None
        for value in (None, True, "900000", 0, -1, 10**400):
            with self.subTest(duration=value):
                info = await self.poll(response(200, self.payload(durationMs=value)))
                self.assertEqual(info["prep_start_time"], self.started)
                self.assertIsNone(info["prep_end_time"])
        for value in (float("inf"), float("nan")):
            with self.subTest(non_finite=value):
                parsed = self.api._parse_car_info_v2(self.payload(durationMs=value))
                self.assertIsNone(parsed["prep_run"].duration_ms)

    async def test_cancel_acceptance_waits_for_telemetry(self):
        active = await self.poll(response(200, self.payload()))
        self.client.async_send_command = AsyncMock(return_value=(True, None))
        with patch.object(self.coordinator, "_schedule_command_refreshes"):
            self.assertTrue(await self.coordinator.async_send_command(
                "car", self.module.COMMAND_CANCEL, "prep_running",
            ))
        self.assertEqual(self.coordinator.data["car"]["prep_end_time"], active["prep_end_time"])
        info = await self.poll(response(200, self.payload(isActive=False, startedAt=None)))
        self.assert_times_unknown(info)
        self.assertFalse(self.coordinator.is_command_pending("car", self.module.COMMAND_CANCEL))
        self.assert_times_unknown(await self.poll(response(304)))

    async def test_finished_run_does_not_return_from_old_telemetry(self):
        await self.poll(response(200, self.payload()))
        finished = await self.poll(response(200, self.payload(finishedAt=self.start_ms + 5_000)))
        self.assertIs(finished["prep_running"], True)
        self.assert_times_unknown(finished)
        self.assert_times_unknown(await self.poll(response(304)))
        self.assert_times_unknown(await self.poll(response(200, self.payload())))

    async def test_estimate_is_fixed_until_new_run_and_304_cannot_extend_it(self):
        original = await self.poll(response(200, self.payload()))
        self.details["prep_script_time"] = 60
        repeated = await self.poll(response(200, self.payload()))
        cached = await self.poll(response(304))
        self.assertEqual(repeated["prep_end_time"], original["prep_end_time"])
        self.assertEqual(cached["prep_end_time"], original["prep_end_time"])
        self.now = self.started + timedelta(minutes=30)
        expired = await self.poll(response(304))
        self.assert_times_unknown(expired)
        self.assertIs(expired["prep_running"], True)
        self.assert_times_unknown(await self.poll(response(200, self.payload())))

        next_start = self.start_ms + 31 * 60_000
        self.now += timedelta(minutes=2)
        new = await self.poll(response(200, self.payload(startedAt=next_start)))
        self.assertEqual(new["prep_start_time"], self.started + timedelta(minutes=31))
        self.assertEqual(new["prep_end_time"], "2026-11-17T17:43:00+00:00")
        self.assert_times_unknown(await self.poll(response(200, self.payload())))
        self.assert_times_unknown(await self.poll(response(304)))

    async def test_failed_poll_and_304_cannot_restore_unconfirmed_times(self):
        original = await self.poll(response(200, self.payload()))
        with self.assertRaises(self.module.UpdateFailed):
            await self.poll(response(503))
        self.assert_times_unknown(self.coordinator.data["car"])
        self.assert_times_unknown(await self.poll(response(304)))
        self.details["prep_script_time"] = 60
        recovered = await self.poll(response(200, self.payload()))
        self.assertEqual(recovered["prep_end_time"], original["prep_end_time"])

    async def test_legacy_keeps_raw_end_time_and_existing_sensor_identifiers(self):
        self.client._imeis.clear()
        legacy = {"isOnline": True, "preparation_script": {
            "running": False, "startTime": str(self.start_ms), "endTime": "legacy-end",
        }}
        client_globals = self.client.async_get_car_info.__func__.__globals__
        dt_util = SimpleNamespace(utc_from_timestamp=lambda value: datetime.fromtimestamp(value, UTC))
        with patch.dict(client_globals, {"dt_util": dt_util}):
            info = await self.poll(response(200, legacy))
        self.assertEqual(info["prep_start_time"], self.started)
        self.assertEqual(info["prep_end_time"], "legacy-end")
        self.assertNotIn("prep_run", info)
        self.assertEqual(await self.poll(response(304)), info)

        platform = load_evolute_module("sensor")
        for key in ("prep_start_time", "prep_end_time"):
            sensor = platform.EvoluteSensor(
                self.coordinator, self.car, key, *platform.SENSOR_TYPES[key],
            )
            self.assertEqual(sensor.unique_id, f"evolute_local-car_{key}")
            self.assertEqual(sensor.entity_id, f"sensor.evolute_local-car_{key}")
            self.assertEqual(sensor.native_value, info[key])
        self.assertEqual(sensor._attr_name, "Trip Preparation End Time")
        self.assertIsNone(sensor._attr_device_class)

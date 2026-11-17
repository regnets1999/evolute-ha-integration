"""Offline tests for the trip preparation duration setting."""

import asyncio
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock

from test_config_flow import load_evolute_module, response


class TripPreparationDurationTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.api = load_evolute_module("client")
        self.module = load_evolute_module("coordinator")
        self.platform = load_evolute_module("select")
        sensor_platform = load_evolute_module("sensor")
        self.session = Mock()
        self.client = self.module.EvoluteClient(self.session, "test-access", "test-refresh")
        self.car = {"car_id": "test/car", "local_id": "local-test", "name": "Fixture"}
        self.car_id = self.car["car_id"]
        self.url = f"{self.api.BASE_URL}/car-service/car/v2/test%2Fcar"
        self.coordinator = self.module.EvoluteDataUpdateCoordinator(Mock(), self.client, [self.car])
        self.details = {"prep_script_time": 10, "prep_script_time_options": [10, 15, 30]}
        self.telemetry = {"prep_duration_supported": True, "prep_running": False}
        self.coordinator._details = {self.car_id: self.details.copy()}
        self.coordinator.data = {self.car_id: {**self.details, **self.telemetry}}
        self.select = self.platform.EvoluteTripPreparationDuration(self.coordinator, self.car)
        sensor_key = "prep_script_time"
        self.sensor = sensor_platform.EvoluteSensor(
            self.coordinator, self.car, sensor_key, *sensor_platform.SENSOR_TYPES[sensor_key]
        )

    def stub_save(self, checked):
        self.session.request.side_effect = [response(204, body=b""), checked]

    def test_options_strict_unique_and_preserve_order(self):
        for raw, expected in (
            ([30, 15, 15, True, 0, -5, "10", None, 20.0, {}, 10], ["30 min", "15 min", "10 min"]),
            (None, []),
            ("10", []),
            ({"10": 10}, []),
            ([], []),
            ([False, 0, "15"], []),
        ):
            with self.subTest(raw=raw):
                self.coordinator._details[self.car_id]["prep_script_time_options"] = raw
                self.assertEqual(self.select.options, expected)
                self.assertEqual(self.select.available, bool(expected))
        self.coordinator._details[self.car_id]["prep_script_time_options"] = [15, 30]
        for current in (10, True, "15", 15.0, None):
            with self.subTest(current=current):
                self.coordinator.data[self.car_id]["prep_script_time"] = current
                self.assertIsNone(self.select.current_option)

    async def test_platform_adds_supported_vehicle_when_data_arrives(self):
        entry = SimpleNamespace(entry_id="entry", async_on_unload=Mock())
        hass = SimpleNamespace(data={self.module.DOMAIN: {"entry": self.coordinator}})
        add_entities = Mock()
        self.coordinator.data = {}
        await self.platform.async_setup_entry(hass, entry, add_entities)
        add_entities.assert_not_called()
        entry.async_on_unload.assert_called_once()
        self.coordinator.data = {self.car_id: self.telemetry}
        self.coordinator.async_update_listeners()
        self.coordinator.async_update_listeners()
        add_entities.assert_called_once()
        selected = add_entities.call_args.args[0][0]
        self.assertEqual(selected.entity_id, "select.evolute_local-test_prep_script_time")
        self.assertEqual(selected.device_info, self.sensor.device_info)
        self.assertEqual(self.sensor.unique_id, "evolute_local-test_prep_script_time")
        self.assertEqual(self.sensor.entity_id, "sensor.evolute_local-test_prep_script_time")

    async def test_put_and_immediate_sensor_update_before_control_read(self):
        async def check_published():
            self.assertEqual(self.sensor.native_value, 15)
            self.assertEqual(self.select.current_option, "15 min")
            return response(200, {"currentScriptTime": 15}).__aenter__.return_value

        control = response(200)
        control.__aenter__.side_effect = check_published
        self.stub_save(control)
        notified = Mock()
        self.coordinator.async_add_listener(notified)
        await self.select.async_select_option("15 min")
        put, get = self.session.request.call_args_list
        self.assertEqual(put.args, ("PUT", self.url))
        self.assertEqual(put.kwargs["json"], {"currentScriptTime": 15})
        self.assertEqual(put.kwargs["headers"]["content-type"], "application/json")
        self.assertEqual(get.args, ("GET", self.url))
        self.assertEqual(self.coordinator._details[self.car_id]["prep_script_time"], 15)
        notified.assert_called()

    async def test_failed_put_does_not_publish_or_read(self):
        before = self.coordinator.data
        self.session.request.return_value = response(403)
        with self.assertRaisesRegex(Exception, "HTTP 403"):
            await self.select.async_select_option("15 min")
        self.assertIs(self.coordinator.data, before)
        self.assertEqual(self.coordinator._details[self.car_id], self.details)
        self.assertEqual(self.sensor.native_value, 10)
        self.session.request.assert_called_once()

    async def test_failed_control_read_keeps_value_accepted_by_put(self):
        for failed_read in (
            response(503), response(200, {}), response(200, {"currentScriptTime": "invalid"}),
        ):
            with self.subTest(checked=failed_read.__aenter__.return_value.status):
                self.stub_save(failed_read)
                await self.select.async_select_option("15 min")
                self.assertEqual(self.sensor.native_value, 15)
                self.assertEqual(self.coordinator._details[self.car_id]["prep_script_time"], 15)

    async def test_control_read_adopts_server_value_and_options(self):
        self.stub_save(response(200, {"currentScriptTime": 30, "availableScriptTime": [15, 30]}))
        await self.select.async_select_option("15 min")
        self.assertEqual(self.sensor.native_value, 30)
        self.assertEqual(self.select.current_option, "30 min")
        self.assertEqual(self.select.options, ["15 min", "30 min"])
        self.assertEqual(self.coordinator._details[self.car_id]["prep_script_time"], 30)

    async def test_write_checks_support_activity_and_latest_options(self):
        for support, active, success in (
            (True, True, True), (None, False, True), (False, False, True), (True, False, False),
        ):
            with self.subTest(support=support, active=active, success=success):
                self.coordinator.data[self.car_id].update(
                    prep_duration_supported=support, prep_running=active,
                )
                self.coordinator.last_update_success = success
                self.assertFalse(self.select.available)
                with self.assertRaisesRegex(Exception, "cannot be changed"):
                    await self.select.async_select_option("15 min")
        self.coordinator.data[self.car_id].update(prep_duration_supported=True, prep_running=False)
        self.coordinator.last_update_success = True
        self.coordinator._details[self.car_id]["prep_script_time_options"] = [30]
        with self.assertRaisesRegex(Exception, "outside the available options"):
            await self.coordinator.async_set_trip_preparation_duration(self.car_id, 15)
        self.session.request.assert_not_called()

    async def test_started_poll_cannot_publish_stale_details_after_save(self):
        old_search_started = asyncio.Event()
        resume_search = asyncio.Event()
        telemetry_started = asyncio.Event()
        resume_telemetry = asyncio.Event()
        old_search = response(200, {"rows": [{
            "_id": self.car_id, "currentScriptTime": 10, "availableScriptTime": [10, 15, 30],
        }]})

        async def get_old_search():
            old_search_started.set()
            await resume_search.wait()
            return old_search.__aenter__.return_value

        async def get_telemetry(_car_id):
            telemetry_started.set()
            await resume_telemetry.wait()
            return self.telemetry

        search = response(200)
        search.__aenter__.side_effect = get_old_search
        self.session.request.side_effect = [search, response(204, body=b""), response(503)]
        self.client.async_get_car_info = AsyncMock(side_effect=get_telemetry)
        poll = asyncio.create_task(self.coordinator._async_poll())
        await asyncio.wait_for(old_search_started.wait(), 2)
        save = asyncio.create_task(self.select.async_select_option("15 min"))
        await asyncio.sleep(0)
        self.assertEqual(self.session.request.call_count, 1)
        resume_search.set()
        await asyncio.wait_for(telemetry_started.wait(), 2)
        await asyncio.wait_for(save, 2)
        resume_telemetry.set()
        published = await asyncio.wait_for(poll, 2)
        self.assertEqual(published[self.car_id]["prep_script_time"], 15)

    async def test_start_waits_for_successful_save_and_control_read(self):
        put_started = asyncio.Event()
        resume_put = asyncio.Event()
        read_started = asyncio.Event()
        resume_read = asyncio.Event()

        async def put_response():
            put_started.set()
            await resume_put.wait()
            return response(204, body=b"").__aenter__.return_value

        async def read_response():
            read_started.set()
            await resume_read.wait()
            return response(200, {"currentScriptTime": 15}).__aenter__.return_value

        put, read = response(204), response(200)
        put.__aenter__.side_effect = put_response
        read.__aenter__.side_effect = read_response
        self.client._imeis[self.car_id] = "test-imei"
        self.client._buttons[self.car_id] = [{"activateCommand": "tripPreparationOn"}]
        self.session.request.side_effect = [put, read, response(200, {})]
        save = asyncio.create_task(self.select.async_select_option("15 min"))
        await asyncio.wait_for(put_started.wait(), 2)
        start = asyncio.create_task(
            self.coordinator.async_send_command(self.car_id, self.api.COMMAND_PREPARE, None)
        )
        await asyncio.sleep(0)
        self.assertEqual(self.session.request.call_count, 1)
        resume_put.set()
        await asyncio.wait_for(read_started.wait(), 2)
        self.assertEqual(self.session.request.call_count, 2)
        resume_read.set()
        await asyncio.wait_for(save, 2)
        self.assertTrue(await asyncio.wait_for(start, 2))
        command = self.session.request.call_args
        self.assertEqual(command.args, ("POST", self.api.TELEMETRY_COMMANDS_URL))
        self.assertEqual(command.kwargs["json"]["durationMinutes"], 15)

    async def test_start_rechecks_admission_after_waiting_for_control_read(self):
        platform = load_evolute_module("button")
        button = platform.EvoluteButton(
            self.coordinator, self.car, "prepare_start", *platform.BUTTON_TYPES["prepare_start"],
        )
        self.client._imeis[self.car_id] = "test-imei"
        self.client._buttons[self.car_id] = [{"activateCommand": "tripPreparationOn"}]
        for condition, expected_error in (
            ("blocked", "currently blocked"),
            ("missing", "vehicle state is unavailable"),
            ("stale", "vehicle state is unavailable"),
        ):
            with self.subTest(condition=condition):
                self.coordinator.data = {self.car_id: {**self.details, **self.telemetry}}
                self.coordinator.last_update_success = True
                self.session.request.reset_mock()
                read_started = asyncio.Event()
                resume_read = asyncio.Event()
                checked = response(200, {"currentScriptTime": 15})

                async def control_read():
                    read_started.set()
                    await resume_read.wait()
                    return checked.__aenter__.return_value

                read = response(200)
                read.__aenter__.side_effect = control_read
                self.session.request.side_effect = [response(204, body=b""), read, response(200, {})]
                save = asyncio.create_task(self.select.async_select_option("15 min"))
                await asyncio.wait_for(read_started.wait(), 2)
                start = asyncio.create_task(button.async_press())
                await asyncio.sleep(0)
                self.assertFalse(start.done())
                if condition == "blocked":
                    self.coordinator.data[self.car_id][self.api.DISABLED_COMMANDS_KEY] = {
                        self.api.COMMAND_PREPARE,
                    }
                elif condition == "missing":
                    self.coordinator.data = {}
                else:
                    self.coordinator.last_update_success = False
                resume_read.set()
                await asyncio.wait_for(save, 2)
                with self.assertRaisesRegex(self.module.HomeAssistantError, expected_error):
                    await asyncio.wait_for(start, 2)
                self.assertEqual(
                    [call.args for call in self.session.request.call_args_list],
                    [("PUT", self.url), ("GET", self.url)],
                )

    async def test_legacy_uses_same_put_and_unmodified_start_body(self):
        legacy = self.api._parse_car_info({"preparation_script": {"available": True, "running": False}})
        self.coordinator.data[self.car_id].update(legacy)
        self.stub_save(response(200, {"currentScriptTime": 15}))
        await self.select.async_select_option("15 min")
        self.assertEqual(self.session.request.call_args_list[0].args, ("PUT", self.url))
        self.session.request.side_effect = None
        self.session.request.return_value = response(200, {})
        self.assertTrue(await self.coordinator.async_send_command(self.car_id, self.api.COMMAND_PREPARE, None))
        call = self.session.request.call_args
        self.assertEqual(call.args, ("POST", f"{self.api.BASE_URL}/car-service/tbox/test/car/PREPARE"))
        self.assertEqual(call.kwargs["json"], {})

    async def test_write_waits_for_start_and_rechecks_preparation_state(self):
        start_sent = asyncio.Event()
        resume_start = asyncio.Event()

        async def send_response():
            start_sent.set()
            await resume_start.wait()
            return response(200, {}).__aenter__.return_value

        start_reply = response(200)
        start_reply.__aenter__.side_effect = send_response
        self.session.request.return_value = start_reply
        start = asyncio.create_task(
            self.coordinator.async_send_command(self.car_id, self.api.COMMAND_PREPARE, None)
        )
        await asyncio.wait_for(start_sent.wait(), 2)
        save = asyncio.create_task(self.select.async_select_option("15 min"))
        await asyncio.sleep(0)
        self.assertFalse(save.done())
        self.coordinator.data[self.car_id]["prep_running"] = True
        resume_start.set()
        self.assertTrue(await asyncio.wait_for(start, 2))
        with self.assertRaisesRegex(Exception, "cannot be changed"):
            await asyncio.wait_for(save, 2)
        self.session.request.assert_called_once()

    def test_capability_requires_boolean_from_matching_telemetry(self):
        for available in (True, False, "true", 1, None):
            with self.subTest(available=available):
                legacy = self.api._parse_car_info({"preparation_script": {"available": available}})
                v2 = self.api._parse_car_info_v2({"buttons": [{
                    "activateCommand": "tripPreparationOn", "enabled": available,
                }]})
                for telemetry in (legacy, v2):
                    self.assertIs(telemetry["prep_duration_supported"], available is True)

"""Offline regressions for trip preparation and carV2 command availability."""

from unittest import IsolatedAsyncioTestCase
from unittest.mock import Mock, patch

from test_config_flow import load_evolute_module, response


class TripPreparationTests(IsolatedAsyncioTestCase):
    async def test_car_v2_trip_preparation_works_without_account_flags(self):
        api = load_evolute_module("client")
        session = Mock()
        client = api.EvoluteClient(session, "test-access", "test-refresh")
        session.request.side_effect = [
            response(200, {"rows": [{"_id": "test-car"}]}),
            response(200, {}),
            response(200, {"imei": "test-imei"}),
        ]
        await client.async_get_cars()

        for running in (False, True):
            with self.subTest(running=running):
                session.get.return_value = response(
                    200,
                    {
                        "isOnline": True,
                        "sensors": {"isParkedOn": True, "isCentralLockingOn": True},
                        "buttons": [{
                            "tag": "trip-preparation",
                            "activateCommand": "tripPreparationOn",
                            "deactivateCommand": "tripPreparationOff",
                            "enabled": True,
                            "status": running,
                            "lastRun": {"isActive": running},
                        }],
                    },
                )

                telemetry = await client.async_get_car_info("test-car")

                self.assertIs(telemetry["prep_available"], True)
                self.assertIs(telemetry["prep_running"], running)
                disabled = telemetry[api.DISABLED_COMMANDS_KEY]
                self.assertEqual(api.COMMAND_PREPARE in disabled, running)
                self.assertEqual(api.COMMAND_CANCEL in disabled, not running)
        session.post.assert_not_called()

    def test_preparing_follows_last_run_and_preserves_control_restrictions(self):
        api = load_evolute_module("client")
        platform = load_evolute_module("binary_sensor")
        coordinator = Mock()
        car = {"car_id": "test-car", "local_id": "local-test-car", "name": "Test car"}
        sensor = platform.EvoluteBinarySensor(
            coordinator, car, "preparing", *platform.BINARY_SENSOR_TYPES["preparing"]
        )
        button = {
            "tag": "trip-preparation", "activateCommand": "tripPreparationOn",
            "deactivateCommand": "tripPreparationOff", "enabled": True,
        }
        raw = {
            "isOnline": True,
            "sensors": {"isParkedOn": True, "isCentralLockingOn": True, "isIgnitionOn": True},
        }
        for status, last_run, expected, controls_running in (
            (False, {"isActive": False}, False, False),
            (False, {"isActive": True}, True, True),
            (True, {"isActive": False, "finishedAt": 1700000000000}, False, False),
            (True, None, None, True),
            (False, {"isActive": "false"}, None, False),
            (False, {}, None, False),
        ):
            with self.subTest(status=status, last_run=last_run):
                row = {**button, "status": status}
                if last_run is not None:
                    row["lastRun"] = last_run
                telemetry = api._parse_car_info_v2({**raw, "buttons": [row]})
                coordinator.data = {"test-car": telemetry}
                self.assertTrue(sensor.available)
                self.assertIs(sensor.is_on, expected)
                disabled = telemetry[api.DISABLED_COMMANDS_KEY]
                self.assertEqual(api.COMMAND_PREPARE in disabled, controls_running)
                self.assertEqual(api.COMMAND_CANCEL in disabled, not controls_running)

        for payload, visible in (
            ({**raw, "buttons": []}, True),
            ({**raw, "buttons": [{**button, "lastRun": {"isActive": True}}]}, False),
        ):
            telemetry = api._parse_car_info_v2(payload, tagged_buttons_visible=visible)
            coordinator.data = {"test-car": telemetry}
            self.assertIsNone(sensor.is_on)
            self.assertTrue({api.COMMAND_PREPARE, api.COMMAND_CANCEL}
                            <= telemetry[api.DISABLED_COMMANDS_KEY])
        coordinator.data = {}
        self.assertFalse(sensor.available)

    def test_unknown_preparing_state_does_not_confirm_pending_command(self):
        module = load_evolute_module("coordinator")
        coordinator = module.EvoluteDataUpdateCoordinator(Mock(), Mock(), [])
        coordinator._pending["test-car"] = {
            module.COMMAND_PREPARE: module.PendingCommand(
                "prep_running", None, module.monotonic() + 45,
            )
        }

        coordinator._resolve_pending({"test-car": {"prep_running": None}})
        self.assertTrue(coordinator.is_command_pending("test-car", module.COMMAND_PREPARE))
        coordinator._resolve_pending({"test-car": {"prep_running": False}})
        self.assertTrue(coordinator.is_command_pending("test-car", module.COMMAND_PREPARE))
        coordinator._resolve_pending({"test-car": {"prep_running": True}})
        self.assertFalse(coordinator.is_command_pending("test-car", module.COMMAND_PREPARE))

    async def test_command_bodies_use_valid_cached_duration_only_for_car_v2_start(self):
        coordinator_module = load_evolute_module("coordinator")
        api = load_evolute_module("client")
        session = Mock()
        client = api.EvoluteClient(session, "test-access", "test-refresh")
        client._imeis["test-car"] = "test-imei"
        client._buttons["test-car"] = [
            {"activateCommand": "tripPreparationOn", "deactivateCommand": "tripPreparationOff"},
            {"activateCommand": "search", "deactivateCommand": "search"},
        ]
        coordinator = coordinator_module.EvoluteDataUpdateCoordinator(Mock(), client, [])

        cases = (
            (api.COMMAND_PREPARE, 15, [5, 15], "tripPreparationOn", 15),
            (api.COMMAND_PREPARE, 15, None, "tripPreparationOn", 15),
            (api.COMMAND_PREPARE, 15, [5], "tripPreparationOn", None),
            (api.COMMAND_PREPARE, 1, [True], "tripPreparationOn", None),
            (api.COMMAND_PREPARE, None, None, "tripPreparationOn", None),
            (api.COMMAND_PREPARE, True, None, "tripPreparationOn", None),
            (api.COMMAND_PREPARE, 0, None, "tripPreparationOn", None),
            (api.COMMAND_PREPARE, -15, None, "tripPreparationOn", None),
            (api.COMMAND_PREPARE, 15.0, None, "tripPreparationOn", None),
            (api.COMMAND_PREPARE, "15", None, "tripPreparationOn", None),
            (api.COMMAND_CANCEL, 15, [15], "tripPreparationOff", None),
            ("blink", 15, [15], "search", None),
        )
        for command, duration, options, name, expected_duration in cases:
            with self.subTest(command=command, duration=duration, options=options):
                coordinator.data = {"test-car": api._parse_car_details({
                    "currentScriptTime": duration, "availableScriptTime": options,
                }), "other-car": {"prep_script_time": 120}}
                session.request.reset_mock()
                session.request.return_value = response(200, {})
                self.assertTrue(await coordinator.async_send_command("test-car", command, None))
                session.request.assert_called_once()
                args, kwargs = session.request.call_args
                self.assertEqual(args, ("POST", api.TELEMETRY_COMMANDS_URL))
                expected = {"commandName": name, "imei": "test-imei"}
                if expected_duration is not None:
                    expected["durationMinutes"] = expected_duration
                self.assertEqual(kwargs["json"], expected)

        for command in (api.COMMAND_PREPARE, api.COMMAND_CANCEL, "blink"):
            with self.subTest(legacy=command):
                coordinator.data = {"legacy-car": {"prep_script_time": 15}}
                session.request.reset_mock()
                self.assertTrue(await coordinator.async_send_command("legacy-car", command, None))
                session.request.assert_called_once()
                args, kwargs = session.request.call_args
                self.assertEqual(args, ("POST", f"{api.BASE_URL}/car-service/tbox/legacy-car/{command}"))
                self.assertEqual(kwargs["json"], {})

    async def test_lock_button_requires_lock_commands_and_preserves_search(self):
        platform = load_evolute_module("button")
        coordinator_module = load_evolute_module("coordinator")
        api = load_evolute_module("client")
        session = Mock()
        client = api.EvoluteClient(session, "test-access", "test-refresh")
        client._imeis["test-car"] = "test-imei"
        car = {"car_id": "test-car", "local_id": "local-test-car", "name": "Test car"}
        coordinator = coordinator_module.EvoluteDataUpdateCoordinator(Mock(), client, [car])
        lock = platform.EvoluteButton(
            coordinator, car, "central_lock_toggle", *platform.BUTTON_TYPES["central_lock_toggle"]
        )
        search = platform.EvoluteButton(
            coordinator, car, "blink", *platform.BUTTON_TYPES["blink"]
        )
        search_row = {
            "tag": "central-lock", "activateCommand": "search",
            "deactivateCommand": "search", "status": False, "enabled": True,
        }
        lock_row = {
            "tag": "central-lock", "activateCommand": "centralLockingOff",
            "deactivateCommand": "centralLockingOn", "status": False, "enabled": True,
        }
        for rows, expected in (
            (None, False),
            ([search_row], False),
            ([search_row, {**lock_row, "deactivateCommand": "search"}], False),
            ([search_row, {**lock_row, "enabled": False}], False),
            ([search_row, lock_row], True),
            ([search_row, {**lock_row, "status": True}], True),
        ):
            with self.subTest(rows=rows):
                raw = {
                    "isOnline": True,
                    "sensors": {"isParkedOn": True, "isCentralLockingOn": True},
                }
                if rows is not None:
                    raw["buttons"] = rows
                session.get.return_value = response(200, raw)
                coordinator.data = {"test-car": await client.async_get_car_info("test-car")}
                self.assertIs(coordinator.data["test-car"]["central_lock"], False)
                self.assertEqual(lock.entity_id, "button.evolute_local-test-car_central_lock_toggle")
                self.assertIs(lock.available, expected)
                self.assertTrue(search.available)
                session.request.reset_mock()
                session.request.return_value = response(200, {})
                if expected:
                    with (
                        patch.object(coordinator, "async_update_listeners", Mock(), create=True),
                        patch.object(coordinator, "_schedule_command_refreshes"),
                    ):
                        await lock.async_press()
                    self.assertEqual(
                        session.request.call_args.kwargs["json"]["commandName"],
                        "centralLockingOn" if rows[-1]["status"] else "centralLockingOff",
                    )
                else:
                    with self.assertRaisesRegex(Exception, "currently blocked"):
                        await lock.async_press()
                    session.request.assert_not_called()
                if rows:
                    await search.async_press()
                    self.assertEqual(session.request.call_args.kwargs["json"]["commandName"], "search")
